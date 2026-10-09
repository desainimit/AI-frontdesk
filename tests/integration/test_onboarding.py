"""Draft provisioning against a real database and its restricted runtime role."""

import asyncio
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.core.errors import AuthenticationError, ConflictError, NotFoundError
from app.events.idempotency import IdempotencyConflictError
from app.identity.sessions import authenticate_platform
from app.locations.models import Location
from app.main import create_app
from app.platform.models import PlatformAuditEvent
from app.platform.onboarding import OnboardingDraft, TenantOnboardingService, UpdateOnboardingDraft
from app.platform.onboarding_models import TenantOnboarding
from app.tenants.models import Tenant
from app.users.models import User
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


def draft() -> OnboardingDraft:
    return OnboardingDraft.model_validate(
        {
            "name": "Draft Clinic",
            "administrator": {"email": "Admin@example.test", "display_name": "Pending Admin"},
            "location": {"name": "Main Clinic", "timezone": "Asia/Kolkata"},
            "language_policy": {"default_language": "en", "enabled_languages": ["en", "hi"]},
        }
    )


async def test_draft_retry_revision_isolation_and_no_credential(
    tenancy: TenancyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = IdentityHarness(tenancy)
    admin_account = await harness.admin()
    # More than one connection is essential to exercise the creation lock.
    database = Database(tenancy.settings.model_copy(update={"db_pool_size": 4}))
    try:
        context = await authenticate_platform(
            database.session_factory, admin_account.token.get_secret_value(), uuid4()
        )
        service = TenantOnboardingService(database.session_factory)
        command, key = draft(), str(uuid4())
        failed_key = str(uuid4())
        failed_command = command.model_copy(update={"name": f"Rollback {uuid4()}"})

        def fail_audit(*_args: object) -> None:
            raise ConflictError()

        # Failure after tenant/admin/location inserts must roll back the whole draft.
        with monkeypatch.context() as patch:
            patch.setattr("app.platform.onboarding._audit", fail_audit)
            with pytest.raises(ConflictError):
                await service.create(context, failed_command, failed_key)
        async with tenancy.admin.begin() as session:
            assert (
                await session.scalar(select(Tenant.id).where(Tenant.name == failed_command.name))
                is None
            )
        assert (await service.create(context, failed_command, failed_key)).revision == 1
        results = await asyncio.gather(*(service.create(context, command, key) for _ in range(4)))
        first = results[0]
        assert all(row == first for row in results)
        assert first.administrator_status == "pending" and first.invitation_status == "not_sent"
        assert first.draft.administrator.email == "admin@example.test"
        assert "token" not in first.model_dump_json()
        with pytest.raises(IdempotencyConflictError):
            await service.create(context, command.model_copy(update={"name": "Changed"}), key)
        other = await service.create(context, command, str(uuid4()))
        assert other.tenant_id != first.tenant_id and other.admin_id != first.admin_id

        changed = UpdateOnboardingDraft.model_validate(
            {**command.model_dump(), "name": "Updated Clinic", "revision": first.revision}
        )
        updated = await service.update(context, first.tenant_id, changed)
        assert updated.revision == 2
        assert await service.update(context, first.tenant_id, changed) == updated
        assert await service.create(context, command, key) == updated
        assert (await service.get(context, other.tenant_id)).draft.name == command.name
        with pytest.raises(ConflictError):
            await service.update(
                context, first.tenant_id, changed.model_copy(update={"name": "Stale update"})
            )
        with pytest.raises(NotFoundError):
            await service.get(context, tenancy.a.tenant_id)  # Legacy tenant has no invented draft.

        async with tenancy.admin.begin() as session:
            admin = await session.get(User, first.admin_id)
            assert admin and not admin.is_active and admin.role == "admin"
            # Invited only: nothing lets this person in until they accept.
            assert admin.accepted_at is None
            location = await session.get(Location, first.location_id)
            assert location and location.tenant_id == first.tenant_id
            assert location.timezone == "Asia/Kolkata"
            tenant = await session.get(Tenant, first.tenant_id)
            assert tenant and tenant.name == "Updated Clinic" and tenant.is_active
            actions = (
                await session.scalars(
                    select(PlatformAuditEvent.action).where(
                        PlatformAuditEvent.tenant_id == first.tenant_id
                    )
                )
            ).all()
            assert actions.count("tenant_onboarding_created") == 1
            assert actions.count("tenant_onboarding_updated") == 1
        for tenant_id in (first.tenant_id, other.tenant_id):
            async with database.session_factory.begin() as session:
                # Tenant-only context cannot read platform-only onboarding, even its own.
                await set_tenant_scope(session, tenant_id)
                assert (await session.scalars(select(TenantOnboarding))).all() == []
                assert set(await session.scalars(select(User.tenant_id))) == {tenant_id}
        async with database.session_factory.begin() as session:
            assert (
                await session.scalar(
                    text("SELECT nullif(current_setting('app.platform_user_id', true), '')")
                )
                is None
            )
            assert (await session.scalars(select(TenantOnboarding))).all() == []
        await harness.identity.logout(admin_account.token.get_secret_value(), uuid4())
        for operation in (
            service.create(context, command, key),
            service.get(context, first.tenant_id),
            service.update(context, first.tenant_id, changed),
        ):
            with pytest.raises(AuthenticationError):
                await operation
    finally:
        await database.close()


async def test_onboarding_over_http_is_for_platform_administrators_only(
    tenancy: TenancyFixture,
) -> None:
    admin = await IdentityHarness(tenancy).admin()
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1/platform/"
        ) as client,
    ):
        client.headers.update(admin.headers)
        client.headers["Idempotency-Key"] = str(uuid4())
        payload = draft().model_dump(mode="json")
        # Not signed in is 401; signed in as a workspace member is 403.
        assert (
            await client.post("tenant-onboardings", json=payload, headers={"Authorization": ""})
        ).status_code == 401
        assert (
            await client.post("tenant-onboardings", json=payload, headers=tenancy.a.headers)
        ).status_code == 403
        missing_key = client.headers.pop("Idempotency-Key")
        assert (await client.post("tenant-onboardings", json=payload)).status_code == 422
        client.headers["Idempotency-Key"] = missing_key
        invalid = {**payload, "provider_secret": "must-not-be-echoed"}
        rejected = await client.post("tenant-onboardings", json=invalid)
        assert rejected.status_code == 422 and "must-not-be-echoed" not in rejected.text
        invalid = {**payload, "location": {"name": "Clinic", "timezone": "Invalid/Timezone"}}
        assert (await client.post("tenant-onboardings", json=invalid)).status_code == 422
        created = await client.post("tenant-onboardings", json=payload)
        assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
        assert "token" not in created.text
        tenant_id = UUID(created.json()["tenant_id"])
        route = f"tenants/{tenant_id}/onboarding"
        assert (await client.get(route)).json() == created.json()
        response = await client.patch(route, json={**payload, "name": "HTTP update", "revision": 1})
        assert response.status_code == 200 and response.json()["revision"] == 2
        assert (await client.patch(route, json={**payload, "revision": 1})).status_code == 409
        assert (await client.get(route, headers=tenancy.b.headers)).status_code == 403
        assert (
            await client.patch(route, json={**payload, "revision": 2}, headers=tenancy.b.headers)
        ).status_code == 403
        # The old way of creating a workspace, which handed out an API token, is gone.
        legacy = await client.post(
            "tenants",
            json={"name": "Legacy", "email": "legacy@example.test", "display_name": "Admin"},
        )
        assert legacy.status_code == 405
