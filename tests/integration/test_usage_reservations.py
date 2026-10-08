"""Real PostgreSQL/isolated financial login; no synthetic provider readiness in public APIs."""

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.auth.context import ActorType, RequestContext
from app.billing.database import FinancialDatabase
from app.billing.models import BillingAccount, BillingLedger
from app.billing.schemas import FinancialAdjustment, MeterPrice, PublishPlan, SelectPlan
from app.billing.service import (
    BillingService,
    InsufficientFundsError,
    PlatformBillingService,
    record_ledger,
)
from app.billing.usage import AdmissionDeniedError, UsageLeaseError, UsageService, budget_window
from app.billing.usage_models import UsageReservation
from app.billing.usage_schemas import ChangeUsage, FencedUsage, ReserveUsage, SettleUsage
from app.configuration.models import PublishedConfiguration
from app.configuration.schemas import OperationalConfiguration
from app.database.rls import set_tenant_scope
from app.errors import AuthorizationError, ConflictError
from app.tenants.models import Tenant
from tests.integration.test_billing_accounts import definition, platform
from tests.integration.test_billing_accounts import financial as financial
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


@dataclass
class Clock:
    now: datetime

    def read(self) -> datetime:
        return self.now


async def publish_fixture(
    tenancy: TenancyFixture,
    tenant_id: UUID,
    *,
    version: int = 1,
    budget: int = 10000,
    enabled: bool = True,
) -> None:
    # Deliberately synthetic publication to exercise the accounting kernel. Normal
    # channel publication remains fail-closed until real dispatch is implemented.
    document = OperationalConfiguration()
    document.business.name = "Synthetic usage admission"
    document.budget.monthly_budget_paisa = budget
    document.voice.enabled = enabled
    value = document.model_dump(mode="json")
    async with tenancy.admin.begin() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.service_state = "live"
        session.add(
            PublishedConfiguration(
                id=uuid4(),
                tenant_id=tenant_id,
                version=version,
                source_revision=version,
                document=value,
                content_hash=hashlib.sha256(json.dumps(value).encode()).hexdigest(),
                published_by=None,
                platform_user_id=None,
            )
        )


async def funded(
    tenancy: TenancyFixture,
    financial: FinancialDatabase,
    *,
    price: int = 2,
    limit: int = 10000,
    concurrency: int = 2,
    funds: int = 10000,
    budget: int = 10000,
) -> tuple[RequestContext, RequestContext, UsageService, Clock]:
    owner = await authenticate(tenancy)
    admin_context = await platform(tenancy)
    admin = PlatformBillingService(tenancy.database.session_factory, financial.session_factory)
    plan_definition = definition(0)
    plan_definition.voice_concurrency = concurrency
    plan_definition.meters = [
        MeterPrice(meter="voice_seconds", price_minor=price, monthly_limit=limit)
    ]
    plan = await admin.publish_plan(
        admin_context,
        PublishPlan(
            mutation_id=uuid4(),
            key="usage-" + uuid4().hex,
            expected_version=0,
            definition=plan_definition,
        ),
    )
    await admin.adjust(
        admin_context,
        owner.tenant_id,
        FinancialAdjustment(
            mutation_id=uuid4(),
            revision=0,
            amount_minor=funds,
            reason="Synthetic usage admission funding",
        ),
    )
    await BillingService(tenancy.database.session_factory, financial.session_factory).select_plan(
        owner,
        SelectPlan(
            mutation_id=uuid4(),
            revision=1,
            plan_version_id=plan.id,
        ),
    )
    await publish_fixture(tenancy, owner.tenant_id, budget=budget)
    clock = Clock(datetime.now(UTC))
    system = RequestContext(uuid4(), owner.tenant_id, None, ActorType.SYSTEM)
    return (
        owner,
        system,
        UsageService(tenancy.database.session_factory, financial.session_factory, clock=clock.read),
        clock,
    )


def reserve_command(units: int = 100) -> ReserveUsage:
    return ReserveUsage(
        mutation_id=uuid4(),
        reservation_id=uuid4(),
        resource_key=uuid4(),
        meter="voice_seconds",
        max_units=units,
    )


async def test_admission_and_settlement_are_exactly_once_and_tenant_isolated(
    tenancy: TenancyFixture, financial: FinancialDatabase
) -> None:
    owner, system, usage, _ = await funded(tenancy, financial)
    command = reserve_command()
    first, second = await asyncio.gather(
        usage.reserve(system, command), usage.reserve(system, command)
    )
    assert first == second and first.quoted_minor == 200
    billing = BillingService(tenancy.database.session_factory)
    assert (await billing.get(owner)).reserved_minor == 200
    with pytest.raises(ConflictError):
        await usage.reserve(system, command.model_copy(update={"max_units": 101}))
    with pytest.raises(AuthorizationError):
        await usage.reserve(owner, reserve_command())
    started = await usage.start(
        system, ChangeUsage(mutation_id=uuid4(), reservation_id=first.id, revision=1)
    )
    assert started.fence is not None
    settlement = SettleUsage(
        mutation_id=uuid4(),
        reservation_id=first.id,
        revision=2,
        fence=started.fence,
        actual_units=40,
        outcome_reference="synthetic-provider-ended-001",
    )
    results = await asyncio.gather(
        usage.settle(system, settlement), usage.settle(system, settlement)
    )
    assert results[0] == results[1] and results[0].state == "settled"
    assert (await billing.get(owner)).balance_minor == 9920
    assert (await billing.get(owner)).reserved_minor == 0
    totals = await usage.get_summary(owner)
    assert totals.spent_minor == 80 and totals.meters[0].consumed_units == 40
    other = await authenticate(tenancy, second=True)
    assert (await usage.get_summary(other)).spent_minor == 0
    async with financial.session_factory.begin() as session:
        await set_tenant_scope(session, other.tenant_id)
        assert await session.get(UsageReservation, first.id) is None
    async with tenancy.admin.begin() as session:
        rows = list(
            await session.scalars(
                select(BillingLedger)
                .where(BillingLedger.tenant_id == owner.tenant_id)
                .order_by(BillingLedger.revision)
            )
        )
        assert [row.kind for row in rows] == ["adjustment", "subscription", "hold", "usage"]
        assert rows[-1].reserved_delta_minor == -200 and rows[-1].amount_minor == -80


async def test_concurrent_admission_enforces_concurrency_budget_units_and_funds(
    tenancy: TenancyFixture, financial: FinancialDatabase
) -> None:
    owner, system, usage, _ = await funded(tenancy, financial, concurrency=1)
    results = await asyncio.gather(
        *(usage.reserve(system, reserve_command()) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(result, AdmissionDeniedError) for result in results) == 1
    assert (await usage.get_summary(owner)).voice_slots_reserved == 1
    held = next(result for result in results if not isinstance(result, BaseException))
    await usage.release(
        system, ChangeUsage(mutation_id=uuid4(), reservation_id=held.id, revision=held.revision)
    )
    # Even though funds and meter capacity remain, the published budget is authoritative.
    await publish_fixture(tenancy, owner.tenant_id, version=2, budget=10)
    with pytest.raises(AdmissionDeniedError):
        await usage.reserve(system, reserve_command(6))
    await publish_fixture(tenancy, owner.tenant_id, version=3, budget=100000)
    with pytest.raises(AdmissionDeniedError):
        await usage.reserve(system, reserve_command(10001))
    with pytest.raises(InsufficientFundsError):
        await usage.reserve(system, reserve_command(5001))
    assert (await BillingService(tenancy.database.session_factory).get(owner)).reserved_minor == 0


async def test_expired_dispatch_keeps_money_and_slots_until_verified_settlement(
    tenancy: TenancyFixture, financial: FinancialDatabase
) -> None:
    owner, system, usage, clock = await funded(tenancy, financial, concurrency=1)
    held = await usage.reserve(system, reserve_command())
    start_command = ChangeUsage(mutation_id=uuid4(), reservation_id=held.id, revision=held.revision)
    running = await usage.start(system, start_command)
    assert running.fence is not None
    with pytest.raises(ConflictError):
        await usage.release(
            system,
            ChangeUsage(mutation_id=uuid4(), reservation_id=running.id, revision=running.revision),
        )
    with pytest.raises(UsageLeaseError):
        await usage.check_dispatch(system, running.id, uuid4())
    clock.now += timedelta(seconds=31)
    with pytest.raises(UsageLeaseError):
        await usage.start(system, start_command)
    with pytest.raises(UsageLeaseError):
        await usage.heartbeat(
            system,
            FencedUsage(
                mutation_id=uuid4(), reservation_id=running.id, revision=2, fence=running.fence
            ),
        )
    assert await usage.expire(system) == 1 and await usage.expire(system) == 0
    totals = await usage.get_summary(owner)
    assert (
        totals.reserved_minor == 200
        and totals.voice_slots_reserved == 1
        and totals.reconciliation_count == 1
    )
    with pytest.raises(AdmissionDeniedError):
        await usage.reserve(system, reserve_command())
    # A confirmed zero-unit terminal outcome releases holds even when the tenant is suspended.
    async with tenancy.admin.begin() as session:
        tenant = await session.get(Tenant, owner.tenant_id)
        assert tenant is not None
        tenant.is_active = False
    await usage.settle(
        system,
        SettleUsage(
            mutation_id=uuid4(),
            reservation_id=running.id,
            revision=3,
            fence=running.fence,
            actual_units=0,
            outcome_reference="synthetic-verified-carrier-termination",
        ),
    )
    async with financial.session_factory.begin() as session:
        await set_tenant_scope(session, owner.tenant_id)
        finished = await session.get(UsageReservation, running.id)
        assert finished is not None and finished.state == "settled"


async def test_unused_expiry_zero_price_and_current_channel_revocation(
    tenancy: TenancyFixture, financial: FinancialDatabase
) -> None:
    owner, system, usage, clock = await funded(tenancy, financial, price=0, concurrency=1, budget=0)
    unused = await usage.reserve(system, reserve_command())
    clock.now += timedelta(seconds=61)
    with pytest.raises(UsageLeaseError):
        await usage.start(
            system, ChangeUsage(mutation_id=uuid4(), reservation_id=unused.id, revision=1)
        )
    assert await usage.expire(system) == 1
    assert (await usage.get_summary(owner)).voice_slots_reserved == 0
    held = await usage.reserve(system, reserve_command())
    running = await usage.start(
        system, ChangeUsage(mutation_id=uuid4(), reservation_id=held.id, revision=1)
    )
    assert running.fence is not None
    await publish_fixture(tenancy, owner.tenant_id, version=2, budget=0, enabled=False)
    with pytest.raises(AdmissionDeniedError):
        await usage.check_dispatch(system, running.id, running.fence)
    with pytest.raises(AdmissionDeniedError):
        await usage.heartbeat(
            system,
            FencedUsage(
                mutation_id=uuid4(), reservation_id=running.id, revision=2, fence=running.fence
            ),
        )
    assert (await usage.get_summary(owner)).voice_slots_reserved == 1
    await usage.settle(
        system,
        SettleUsage(
            mutation_id=uuid4(),
            reservation_id=running.id,
            revision=2,
            fence=running.fence,
            actual_units=10,
            outcome_reference="synthetic-free-meter-closed",
        ),
    )
    totals = await usage.get_summary(owner)
    assert totals.voice_slots_reserved == 0 and totals.meters[0].consumed_units == 10
    assert totals.spent_minor == 0


async def test_database_reservation_projection_and_history_cannot_be_forged(
    tenancy: TenancyFixture, financial: FinancialDatabase
) -> None:
    owner, system, usage, _ = await funded(tenancy, financial)
    held = await usage.reserve(system, reserve_command())
    with pytest.raises(DBAPIError):
        async with financial.session_factory.begin() as session:
            await set_tenant_scope(session, owner.tenant_id)
            await session.execute(
                text("UPDATE usage_reservations SET quoted_minor = 1 WHERE id = :id"),
                {"id": held.id},
            )
    with pytest.raises(DBAPIError):
        async with financial.session_factory.begin() as session:
            await set_tenant_scope(session, owner.tenant_id)
            await session.execute(
                text(
                    "UPDATE usage_reservations SET revision = revision + 1, state = 'released', "
                    "actual_units = 0, settled_at = now(), outcome_reference = 'forged' "
                    "WHERE id = :id"
                ),
                {"id": held.id},
            )
    async with tenancy.database.session_factory.begin() as session:
        assert (
            await session.scalar(
                text("SELECT has_table_privilege(current_user, 'usage_reservations', 'INSERT')")
            )
            is False
        )
    assert (await usage.get_summary(owner)).reserved_minor == 200
    # A mathematically valid ledger entry still cannot invent an unreceipted usage charge.
    with pytest.raises(DBAPIError):
        async with financial.session_factory.begin() as session:
            await set_tenant_scope(session, owner.tenant_id)
            account = await session.scalar(
                select(BillingAccount).where(BillingAccount.tenant_id == owner.tenant_id)
            )
            assert account is not None
            record_ledger(
                session,
                account,
                mutation_id=uuid4(),
                kind="usage",
                amount=-1,
                reason="Rejected unreceipted usage charge",
                request_id=uuid4(),
            )
    start, end = budget_window(datetime(2026, 10, 31, 23, 59, tzinfo=UTC), "Asia/Kolkata")
    assert start == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)
    assert end == datetime(2026, 11, 30, 18, 30, tzinfo=UTC)
