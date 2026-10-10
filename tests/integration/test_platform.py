"""Platform administrators: bootstrap, sign-in, workspace management and database limits."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.database.rls import set_tenant_scope
from app.core.errors import AuthenticationError
from app.identity.models import Account, AccountSession
from app.identity.passwords import verify_password
from app.identity.sessions import authenticate_platform, platform_scope
from app.main import create_app
from app.patients.models import Patient
from app.platform.bootstrap import seed_super_admin
from app.platform.models import PlatformAuditEvent
from app.platform.schemas import SuperAdminSeed
from app.tenants.models import Tenant
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


def seed_command() -> SuperAdminSeed:
    return SuperAdminSeed(
        email=f"{uuid4()}@example.test",
        password=SecretStr("Test-only super-admin password!"),
        display_name="Test Super Admin",
    )


async def test_bootstrap_is_concurrent_idempotent_and_never_resets(tenancy: TenancyFixture) -> None:
    command = seed_command()
    results = await asyncio.gather(
        seed_super_admin(tenancy.admin, command), seed_super_admin(tenancy.admin, command)
    )
    assert sorted(results) == [False, True]
    async with tenancy.admin.begin() as session:
        account = await session.scalar(select(Account).where(Account.email == command.email))
        assert account and account.is_platform_admin and account.password_hash
        assert verify_password(command.password.get_secret_value(), account.password_hash)
        identity, original_hash = account.id, account.password_hash
        account.is_active = False
    # Running setup again never changes a password or re-enables a disabled account.
    assert not await seed_super_admin(
        tenancy.admin,
        command.model_copy(update={"password": SecretStr("Replacement password not applied")}),
    )
    # Nor does it promote an existing workspace member who happens to use that address.
    assert not await seed_super_admin(
        tenancy.admin, command.model_copy(update={"email": tenancy.a.email})
    )
    async with tenancy.admin.begin() as session:
        account = await session.get(Account, identity)
        assert account and not account.is_active and account.password_hash == original_hash
        member = await session.get(Account, tenancy.a.account_id)
        assert member and not member.is_platform_admin
        audit = (
            await session.scalars(
                select(PlatformAuditEvent).where(PlatformAuditEvent.user_id == identity)
            )
        ).all()
        assert len(audit) == 1 and audit[0].action == "super_admin_created"


async def test_platform_administrator_signs_in_and_manages_workspaces(
    tenancy: TenancyFixture,
) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    harness = IdentityHarness(tenancy)
    app = create_app(harness.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        assert (await client.get("platform/tenants")).status_code == 401
        payload = {"email": command.email, "password": "wrong"}
        assert (await client.post("auth/login", json=payload)).status_code == 401
        payload["password"] = command.password.get_secret_value()
        # The same sign-in page and route as everyone else, with no code step by default.
        response = await client.post("auth/login", json=payload)
        assert response.status_code == 200 and response.json()["next"] == "platform"
        assert response.headers["cache-control"] == "no-store"
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        me = (await client.get("auth/me")).json()
        assert me["account"]["is_platform_admin"] and me["workspace"] is None
        assert me["next"] == "platform" and "password" not in str(me)
        assert (await client.get("auth/workspaces")).json() == []
        # Without opening a workspace there is no workspace to act in.
        assert (await client.get("tenants/current")).status_code == 409

        # Oldest first: read every page so the result does not depend on how many
        # workspaces earlier tests left in the shared database.
        listed: set[str] = set()
        cursor: str | None = ""
        while cursor is not None:
            page = (await client.get(f"platform/tenants?limit=100&cursor={cursor}")).json()
            listed.update(t["id"] for t in page["items"])
            cursor = page["next_cursor"]
        assert {str(tenancy.a.tenant_id), str(tenancy.b.tenant_id)}.issubset(listed)

        tenant_id = str(tenancy.a.tenant_id)
        member = tenancy.a.headers
        assert (await client.get("tenants/current", headers=member)).status_code == 200
        assert (
            await client.patch(f"platform/tenants/{tenant_id}", json={"is_active": False})
        ).status_code == 200
        # A closed workspace is no longer a place its members can work in.
        assert (await client.get("tenants/current", headers=member)).status_code == 409
        assert (
            await client.patch(f"platform/tenants/{tenant_id}", json={"is_active": True})
        ).status_code == 200
        assert (await client.get("tenants/current", headers=member)).status_code == 200
        assert (await client.patch(f"platform/tenants/{tenant_id}", json={})).status_code == 422
        assert (await client.post("auth/logout")).status_code == 204
        assert (await client.get("platform/tenants")).status_code == 401
        assert (await client.get("auth/me")).status_code == 401
    async with tenancy.admin.begin() as session:
        account = await session.scalar(select(Account).where(Account.email == command.email))
        assert account
        actions = (
            await session.scalars(
                select(PlatformAuditEvent.action).where(PlatformAuditEvent.user_id == account.id)
            )
        ).all()
        assert set(actions) == {
            "super_admin_created",
            "signed_in",
            "signed_out",
            "tenants_viewed",
            "tenant_deactivated",
            "tenant_activated",
        }


@pytest.mark.parametrize("invalid", ["disabled", "expired", "forged", "demoted"])
async def test_invalid_platform_sessions_rejected(tenancy: TenancyFixture, invalid: str) -> None:
    harness = IdentityHarness(tenancy)
    admin = await harness.admin()
    token = admin.token.get_secret_value()
    context = await harness.platform(admin)
    async with tenancy.admin.begin() as session:
        if invalid == "disabled":
            await session.execute(
                update(Account).where(Account.id == admin.account_id).values(is_active=False)
            )
        elif invalid == "expired":
            await session.execute(
                update(AccountSession)
                .where(AccountSession.id == admin.session_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        elif invalid == "demoted":
            await session.execute(
                update(Account)
                .where(Account.id == admin.account_id)
                .values(is_platform_admin=False)
            )
        else:
            token = token[:-1] + ("A" if token[-1] != "A" else "B")
    if invalid != "forged":
        # A context issued before the change is refused inside its next transaction.
        with pytest.raises(AuthenticationError):
            async with tenancy.database.session_factory.begin() as session:
                await platform_scope(session, context)
    if invalid == "demoted":
        return  # Still a valid sign-in, just no longer a platform administrator.
    with pytest.raises(AuthenticationError):
        await authenticate_platform(tenancy.database.session_factory, token, uuid4())


async def test_platform_scope_does_not_bypass_clinical_rls_or_leak_into_pool(
    tenancy: TenancyFixture,
) -> None:
    context = await IdentityHarness(tenancy).platform()
    async with tenancy.admin.begin() as session:
        session.add(
            Patient(tenant_id=tenancy.a.tenant_id, display_name="Private", phone="+919999999999")
        )
    async with tenancy.database.session_factory.begin() as session:
        await platform_scope(session, context)
        assert tenancy.a.tenant_id in (await session.scalars(select(Tenant.id))).all()
        # Managing workspaces is not reading what is inside them.
        assert (await session.scalars(select(Patient))).all() == []
    async with tenancy.database.session_factory.begin() as session:
        assert (
            await session.scalar(
                text("SELECT nullif(current_setting('app.platform_user_id', true), '')")
            )
            is None
        )
        assert (await session.scalars(select(Tenant))).all() == []
        await set_tenant_scope(session, tenancy.a.tenant_id)
        assert (await session.scalars(select(Tenant.id))).all() == [tenancy.a.tenant_id]


async def test_api_login_cannot_promote_accounts_or_touch_the_activity_log(
    tenancy: TenancyFixture,
) -> None:
    for sql in (
        # The administrator flag is outside what the API login may write.
        "INSERT INTO accounts (id, email, display_name, is_platform_admin) "
        "VALUES (gen_random_uuid(), 'bad@example.test', 'Bad', true)",
        "UPDATE accounts SET is_platform_admin = true",
        "UPDATE accounts SET email = 'taken@example.test'",
        "UPDATE accounts SET is_active = true",
        "DELETE FROM accounts",
        "DELETE FROM sessions",
        "UPDATE sessions SET account_id = gen_random_uuid()",
        "UPDATE sessions SET token_digest = repeat('0', 64)",
        # The activity log is write-only for the API.
        "SELECT action FROM platform_audit_events",
        "DELETE FROM platform_audit_events",
    ):
        with pytest.raises(DBAPIError):
            async with tenancy.database.session_factory.begin() as session:
                await session.execute(text(sql))
    await seed_super_admin(tenancy.admin, seed_command())
    # Even the schema owner cannot rewrite history through ordinary SQL.
    for sql in (
        "UPDATE platform_audit_events SET action='changed'",
        "DELETE FROM platform_audit_events",
        "TRUNCATE platform_audit_events",
    ):
        with pytest.raises(DBAPIError):
            async with tenancy.admin.begin() as session:
                await session.execute(text(sql))
