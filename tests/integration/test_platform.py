import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.api.platform import login_limit
from app.cache.redis import RedisClient
from app.database.rls import set_tenant_scope
from app.errors import AuthenticationError
from app.main import create_app
from app.patients.models import Patient
from app.platform.bootstrap import seed_super_admin
from app.platform.mfa import totp
from app.platform.models import PlatformAuditEvent, PlatformSession, PlatformUser
from app.platform.passwords import verify_password
from app.platform.schemas import SuperAdminSeed
from app.platform.service import PlatformAuthenticationService
from app.platform.tenants import platform_scope
from app.platform.throttle import LoginThrottledError, check_login_limit
from app.tenants.models import Tenant
from tests.fakes import FakeDependency
from tests.integration.platform_mfa_helpers import mfa_login, mfa_settings
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
        user = await session.scalar(select(PlatformUser).where(PlatformUser.email == command.email))
        assert user and user.role == "super_admin"
        assert verify_password(command.password.get_secret_value(), user.password_hash)
        identity, original_hash = user.id, user.password_hash
        user.is_active = False
    assert not await seed_super_admin(
        tenancy.admin,
        command.model_copy(update={"password": SecretStr("Replacement password not applied")}),
    )
    async with tenancy.admin.begin() as session:
        user = await session.get(PlatformUser, identity)
        assert user and not user.is_active and user.password_hash == original_hash
        audit = (
            await session.scalars(
                select(PlatformAuditEvent).where(PlatformAuditEvent.user_id == identity)
            )
        ).all()
        assert len(audit) == 1 and audit[0].action == "super_admin_created"


async def test_platform_http_login_manage_tenants_logout(tenancy: TenancyFixture) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    app = create_app(mfa_settings(tenancy.settings), redis_factory=lambda _: FakeDependency())

    async def allow() -> None:
        pass  # Shared Redis throttling is tested separately below.

    app.dependency_overrides[login_limit] = allow
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        assert (await client.get("platform/tenants")).status_code == 401
        payload = {"email": command.email, "password": "wrong"}
        assert (await client.post("platform/auth/login", json=payload)).status_code == 401
        payload["password"] = command.password.get_secret_value()
        response = await client.post("platform/auth/login", json=payload)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        token = response.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        response = await client.get("platform/me")
        assert response.status_code == 200 and response.json()["role"] == "super_admin"
        assert "password" not in response.text
        assert response.json()["mfa_verified"] is False
        assert (await client.get("platform/tenants")).status_code == 403
        enrolled = await client.post("platform/auth/mfa/enroll")
        assert enrolled.status_code == 200
        code = totp(enrolled.json()["secret"], int(datetime.now(UTC).timestamp()) // 30)
        assert (
            await client.post("platform/auth/mfa/confirm", json={"code": code})
        ).status_code == 204
        assert (await client.get("platform/me")).json()["mfa_verified"] is True
        # Oldest first: read every page so the result does not depend on how many
        # tenants earlier tests left in the shared database.
        listed: set[str] = set()
        for offset in range(0, 10001, 100):
            page = (await client.get(f"platform/tenants?limit=100&offset={offset}")).json()
            listed.update(t["id"] for t in page)
            if len(page) < 100:
                break
        assert {str(tenancy.a.tenant_id), str(tenancy.b.tenant_id)}.issubset(listed)
        response = await client.post(
            "platform/tenants",
            json={
                "name": "Platform-created",
                "email": "tenant-admin@example.test",
                "display_name": "Tenant Admin",
            },
        )
        assert response.status_code == 201
        created = response.json()
        tenant_id = created["tenant_id"]
        tenant_token = created["admin_token"]
        assert (
            await client.get("tenants/current", headers={"Authorization": f"Bearer {tenant_token}"})
        ).status_code == 200
        assert (
            await client.get("platform/me", headers={"Authorization": f"Bearer {tenant_token}"})
        ).status_code == 401
        assert (await client.get("patients")).status_code == 401
        assert (
            await client.patch(f"platform/tenants/{tenant_id}", json={"is_active": False})
        ).status_code == 200
        assert (
            await client.get("tenants/current", headers={"Authorization": f"Bearer {tenant_token}"})
        ).status_code == 401
        assert (
            await client.patch(f"platform/tenants/{tenant_id}", json={"is_active": True})
        ).status_code == 200
        renamed = await client.patch(f"platform/tenants/{tenant_id}", json={"name": " New Name "})
        assert renamed.status_code == 200 and renamed.json()["name"] == "New Name"
        assert renamed.json()["is_active"] is True
        assert (await client.patch(f"platform/tenants/{tenant_id}", json={})).status_code == 422
        assert (
            await client.get("tenants/current", headers={"Authorization": f"Bearer {tenant_token}"})
        ).status_code == 200
        assert (await client.post("platform/auth/logout")).status_code == 204
        assert (await client.get("platform/me")).status_code == 401
    async with tenancy.admin.begin() as session:
        user = await session.scalar(select(PlatformUser).where(PlatformUser.email == command.email))
        assert user
        actions = (
            await session.scalars(
                select(PlatformAuditEvent.action).where(PlatformAuditEvent.user_id == user.id)
            )
        ).all()
        assert set(actions) == {
            "super_admin_created",
            "super_admin_login",
            "super_admin_logout",
            "platform_mfa_enrollment_started",
            "platform_mfa_enrolled",
            "tenants_viewed",
            "tenant_created",
            "tenant_deactivated",
            "tenant_activated",
            "tenant_renamed",
        }


@pytest.mark.parametrize("invalid", ["disabled", "expired", "forged"])
async def test_invalid_platform_sessions_rejected(tenancy: TenancyFixture, invalid: str) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    service = PlatformAuthenticationService(tenancy.database.session_factory)
    token = (await mfa_login(service, command, tenancy.settings)).access_token
    context = await service.authenticate(token, uuid4())
    async with tenancy.admin.begin() as session:
        if invalid == "disabled":
            await session.execute(
                update(PlatformUser)
                .where(PlatformUser.id == context.user_id)
                .values(is_active=False)
            )
        elif invalid == "expired":
            await session.execute(
                update(PlatformSession)
                .where(PlatformSession.id == context.session_id)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        else:
            token = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(AuthenticationError):
        await service.authenticate(token, uuid4())


async def test_platform_scope_does_not_bypass_clinical_rls_or_leak_into_pool(
    tenancy: TenancyFixture,
) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    service = PlatformAuthenticationService(tenancy.database.session_factory)
    token = (await mfa_login(service, command, tenancy.settings)).access_token
    context = await service.authenticate(token, uuid4())
    async with tenancy.admin.begin() as session:
        session.add(
            Patient(tenant_id=tenancy.a.tenant_id, display_name="Private", phone="+919999999999")
        )
    async with tenancy.database.session_factory.begin() as session:
        await platform_scope(session, context)
        assert tenancy.a.tenant_id in (await session.scalars(select(Tenant.id))).all()
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


async def test_runtime_cannot_promote_accounts_and_audit_cannot_change(
    tenancy: TenancyFixture,
) -> None:
    for sql in (
        "INSERT INTO platform_users (id, email, display_name, password_hash, role) "
        "VALUES (gen_random_uuid(), 'bad@example.test', 'Bad', 'bad', 'super_admin')",
        "UPDATE platform_users SET password_hash='bad'",
        "DELETE FROM platform_audit_events",
        "UPDATE platform_sessions SET expires_at=now() + interval '1 year'",
    ):
        with pytest.raises(DBAPIError):
            async with tenancy.database.session_factory.begin() as session:
                await session.execute(text(sql))
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    for sql in (
        "UPDATE platform_audit_events SET action='changed'",
        "DELETE FROM platform_audit_events",
        "TRUNCATE platform_audit_events",
    ):
        with pytest.raises(DBAPIError):
            async with tenancy.admin.begin() as session:
                await session.execute(text(sql))


@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "true", reason="requires real Redis")
async def test_platform_login_throttle_is_shared_and_expires(tenancy: TenancyFixture) -> None:
    redis = RedisClient(tenancy.settings)
    email, address = f"{uuid4()}@example.test", str(uuid4())
    try:
        for _ in range(5):
            await check_login_limit(redis, email, address)
        with pytest.raises(LoginThrottledError):
            await check_login_limit(redis, email, address)
    finally:
        await redis.close()
