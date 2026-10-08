"""Financial writes use a separate login and immutable, revisioned accounting."""

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.billing.database import FinancialDatabase, FinancialUnavailableError
from app.billing.models import BillingAccount, BillingLedger
from app.billing.schemas import (
    FinancialAdjustment,
    MeterPrice,
    PlanDefinition,
    PublishPlan,
    SelectPlan,
)
from app.billing.service import (
    BillingService,
    InsufficientFundsError,
    InvalidBillingCursorError,
    PlatformBillingService,
    SubscriptionLockedError,
    next_month,
)
from app.database.rls import set_tenant_scope
from app.errors import AuthorizationError, ConflictError, NotFoundError
from app.main import create_app
from app.onboarding.service import SetupService
from app.platform.bootstrap import seed_super_admin
from app.platform.policies import PlatformContext
from app.platform.service import PlatformAuthenticationService
from app.rbac.service import RevisionConflictError
from app.security.encryption import SecretCipher
from tests.integration.platform_mfa_helpers import mfa_login, mfa_settings
from tests.integration.test_platform import seed_command
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


@pytest.fixture
async def financial(tenancy: TenancyFixture) -> AsyncGenerator[FinancialDatabase]:
    database = FinancialDatabase(tenancy.settings)
    await database.ping()
    try:
        yield database
    finally:
        await database.close()


async def platform(tenancy: TenancyFixture) -> PlatformContext:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    settings = mfa_settings(tenancy.settings)
    authentication = PlatformAuthenticationService(tenancy.database.session_factory, settings)
    login = await mfa_login(authentication, command, settings)
    return await authentication.authenticate(login.access_token, uuid4())


def definition(price: int = 100) -> PlanDefinition:
    return PlanDefinition(
        name="Evaluated test plan",
        description="Synthetic test prices only",
        subscription_price_minor=price,
        voice_concurrency=2,
        entitlements=["voice"],
        meters=[MeterPrice(meter="voice_seconds", price_minor=2, monthly_limit=10000)],
    )


async def test_subscription_and_funds_are_idempotent_isolated_and_version_pinned(
    tenancy: TenancyFixture,
    financial: FinancialDatabase,
) -> None:
    owner = await authenticate(tenancy)
    other = await authenticate(tenancy, second=True)
    context = await platform(tenancy)
    admin = PlatformBillingService(tenancy.database.session_factory, financial.session_factory)
    billing = BillingService(tenancy.database.session_factory, financial.session_factory)
    publish = PublishPlan(
        mutation_id=uuid4(), key="test-" + uuid4().hex, expected_version=0, definition=definition()
    )
    with pytest.raises(AuthorizationError):
        await admin.publish_plan(replace(context, mfa_verified=False), publish)
    plan = await admin.publish_plan(context, publish)
    assert await admin.publish_plan(context, publish) == plan
    assert (await billing.get(owner)).revision == 0
    adjustment = FinancialAdjustment(
        mutation_id=uuid4(),
        revision=0,
        amount_minor=10000,
        reason="Synthetic initial balance adjustment",
    )
    results = await asyncio.gather(
        admin.adjust(context, owner.tenant_id, adjustment),
        admin.adjust(context, owner.tenant_id, adjustment),
    )
    assert results[0] == results[1] and results[0].revision == 1
    with pytest.raises(ConflictError):
        await admin.adjust(
            context, owner.tenant_id, adjustment.model_copy(update={"amount_minor": 10001})
        )
    command = SelectPlan(mutation_id=uuid4(), revision=1, plan_version_id=plan.id)
    selected = await billing.select_plan(owner, command)
    assert await billing.select_plan(owner, command) == selected
    assert await billing.mutation(owner, command.mutation_id) == selected
    view = await billing.get(owner)
    assert view.funding_ready and view.balance_minor == 9900 and view.revision == 2
    assert view.subscription and view.subscription.plan == plan and view.subscription.active
    newer = await admin.publish_plan(
        context,
        publish.model_copy(
            update={"mutation_id": uuid4(), "expected_version": 1, "definition": definition(999)}
        ),
    )
    assert newer.version == 2 and any(row.id == newer.id for row in await billing.catalog(owner))
    assert (await billing.get(owner)).subscription == view.subscription
    with pytest.raises(SubscriptionLockedError):
        await billing.select_plan(
            owner,
            command.model_copy(
                update={"mutation_id": uuid4(), "revision": 2, "plan_version_id": newer.id}
            ),
        )
    assert (await billing.get(other)).balance_minor == 0
    with pytest.raises(NotFoundError):
        await billing.mutation(other, command.mutation_id)
    setup = await SetupService(tenancy.database.session_factory).get_setup(owner)
    assert next(step for step in setup.steps if step.key == "billing").status == "needs_review"
    async with tenancy.admin.begin() as session:
        rows = list(
            await session.scalars(
                select(BillingLedger)
                .where(BillingLedger.tenant_id == owner.tenant_id)
                .order_by(BillingLedger.revision)
            )
        )
        assert [row.amount_minor for row in rows] == [10000, -100]
        assert rows[0].platform_user_id == context.user_id and rows[1].actor_id == owner.actor_id


async def test_conflicts_and_overdrafts_do_not_change_money(
    tenancy: TenancyFixture,
    financial: FinancialDatabase,
) -> None:
    owner = await authenticate(tenancy)
    context = await platform(tenancy)
    admin = PlatformBillingService(tenancy.database.session_factory, financial.session_factory)
    billing = BillingService(tenancy.database.session_factory, financial.session_factory)
    commands = [
        FinancialAdjustment(
            mutation_id=uuid4(),
            revision=0,
            amount_minor=100,
            reason="Synthetic concurrent credit adjustment",
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *(admin.adjust(context, owner.tenant_id, command) for command in commands),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflictError) for result in results) == 1
    assert (await billing.get(owner)).balance_minor == 100
    with pytest.raises(InsufficientFundsError):
        await admin.adjust(
            context,
            owner.tenant_id,
            FinancialAdjustment(
                mutation_id=uuid4(),
                revision=1,
                amount_minor=-101,
                reason="Rejected synthetic overdraft adjustment",
            ),
        )
    assert (await billing.get(owner)).revision == 1
    with pytest.raises(FinancialUnavailableError):
        await PlatformBillingService(tenancy.database.session_factory).adjust(
            context, owner.tenant_id, commands[0]
        )


async def test_database_roles_and_projection_reject_financial_bypass(
    tenancy: TenancyFixture,
    financial: FinancialDatabase,
) -> None:
    owner = await authenticate(tenancy)
    context = await platform(tenancy)
    admin = PlatformBillingService(tenancy.database.session_factory, financial.session_factory)
    await admin.adjust(
        context,
        owner.tenant_id,
        FinancialAdjustment(
            mutation_id=uuid4(),
            revision=0,
            amount_minor=100,
            reason="Synthetic database privilege test funding",
        ),
    )
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, owner.tenant_id)
        assert (
            await session.scalar(
                text("SELECT has_table_privilege(current_user, 'billing_ledger', 'INSERT')")
            )
            is False
        )
        assert (
            await session.scalar(
                text("SELECT has_table_privilege(current_user, 'billing_accounts', 'UPDATE')")
            )
            is False
        )
    with pytest.raises(DBAPIError):
        async with financial.session_factory.begin() as session:
            await set_tenant_scope(session, owner.tenant_id)
            await session.execute(
                text(
                    "UPDATE billing_accounts SET balance_minor = balance_minor + 1 "
                    "WHERE tenant_id = :tenant"
                ),
                {"tenant": owner.tenant_id},
            )
    with pytest.raises(DBAPIError):
        async with financial.session_factory.begin() as session:
            await set_tenant_scope(session, owner.tenant_id)
            await session.execute(
                text("UPDATE billing_ledger SET reason = 'forged' WHERE tenant_id = :tenant"),
                {"tenant": owner.tenant_id},
            )
    with pytest.raises(DBAPIError):
        async with financial.session_factory.begin() as session:
            await session.execute(text("SELECT * FROM integration_credential_versions LIMIT 1"))
    async with financial.session_factory.begin() as session:
        await set_tenant_scope(session, tenancy.b.tenant_id)
        assert (
            await session.scalar(
                select(BillingAccount).where(BillingAccount.tenant_id == owner.tenant_id)
            )
            is None
        )
    assert (await BillingService(tenancy.database.session_factory).get(owner)).balance_minor == 100


async def test_ledger_seek_cursor_is_bound_to_tenant_authority_and_page_size(
    tenancy: TenancyFixture,
    financial: FinancialDatabase,
) -> None:
    owner = await authenticate(tenancy)
    other = await authenticate(tenancy, second=True)
    context = await platform(tenancy)
    admin = PlatformBillingService(tenancy.database.session_factory, financial.session_factory)
    for revision in range(27):
        await admin.adjust(
            context,
            owner.tenant_id,
            FinancialAdjustment(
                mutation_id=uuid4(),
                revision=revision,
                amount_minor=1,
                reason="Synthetic ledger pagination adjustment",
            ),
        )
    settings = mfa_settings(tenancy.settings)
    # Key IDs are not restricted to a dot-free token format.
    keys = json.loads(settings.secret_encryption_keys.get_secret_value())
    cipher = SecretCipher(
        SecretStr(json.dumps({"billing.key.v1": next(iter(keys.values()))})), "billing.key.v1"
    )
    service = BillingService(tenancy.database.session_factory)
    first = await service.ledger(owner, cipher)
    assert len(first.items) == 25 and first.next_cursor
    second = await service.ledger(owner, cipher, cursor=first.next_cursor)
    assert [item.revision for item in second.items] == [2, 1] and second.next_cursor is None
    assert not {item.id for item in first.items}.intersection(item.id for item in second.items)
    for context_to_try, limit in ((other, 25), (owner, 50)):
        with pytest.raises(InvalidBillingCursorError):
            await service.ledger(context_to_try, cipher, cursor=first.next_cursor, limit=limit)
    assert next_month(datetime(2028, 1, 31, tzinfo=UTC)).day == 29


async def test_http_catalog_permissions_and_strict_financial_commands(
    tenancy: TenancyFixture,
) -> None:
    settings = mfa_settings(tenancy.settings)
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
        overview = await client.get("/api/v2/billing", headers=headers)
        assert overview.status_code == 200 and overview.json()["balance_minor"] == 0
        for limit in (25, 50, 100):
            response = await client.get(
                "/api/v2/billing/ledger", headers=headers, params={"limit": limit}
            )
            assert response.status_code == 200 and response.json()["items"] == []
        catalog = await client.get("/api/v2/billing/catalog", headers=headers)
        assert catalog.status_code == 200 and catalog.json()["payments_available"] is False
        usage = await client.get("/api/v2/billing/usage", headers=headers)
        assert usage.status_code == 200 and usage.json()["reserved_minor"] == 0
        assert usage.json()["meters"] == []
        assert (
            await client.post("/api/v2/billing/usage", headers=headers, json={"state": "settled"})
        ).status_code == 405
        assert (
            await client.get("/api/v2/platform/billing/plans", headers=headers)
        ).status_code == 401
        command = {"mutation_id": str(uuid4()), "revision": 0, "plan_version_id": str(uuid4())}
        assert (
            await client.post(
                "/api/v2/billing/subscription",
                headers=headers,
                json={**command, "amount_minor": -1},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/api/v2/billing/subscription", headers=headers, json={**command, "revision": True}
            )
        ).status_code == 422
        assert (
            await client.get("/api/v2/billing/ledger", headers=headers, params={"limit": 24})
        ).status_code == 422
        assert (
            await client.get("/api/v2/billing/ledger", headers=headers, params={"kind": "anything"})
        ).status_code == 422
