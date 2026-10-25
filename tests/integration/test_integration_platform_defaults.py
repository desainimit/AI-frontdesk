"""Explicit super-admin reset to platform accounts, using restricted PostgreSQL roles."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.audit.models import AuditEvent
from app.calls.models import PhoneNumber
from app.calls.service import CallBinding, CallService
from app.core.errors import AuthorizationError, ConflictError, NotFoundError
from app.integrations.models import CredentialVersion
from app.integrations.service import (
    BindNumber,
    PlatformActor,
    RevisionCommand,
    SubmitCredentials,
    UpdateIntegration,
    VerifyCommand,
)
from app.integrations.verifiers import ProviderNumber
from app.main import create_app
from app.platform.models import PlatformAuditEvent
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness
from tests.integration.test_integrations import Harness, account_sid, auth_token, phone
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


@pytest.mark.parametrize("revoked", [False, True])
async def test_clear_twilio_override_and_require_platform_number(
    tenancy: TenancyFixture, revoked: bool
) -> None:
    harness = Harness(
        tenancy, twilio_account_sid=account_sid(), twilio_auth_token=SecretStr(auth_token())
    )
    tenant_id = tenancy.a.tenant_id
    owner = tenancy.context(tenancy.a)
    identity = IdentityHarness(tenancy)
    platform = await identity.platform()
    actor = PlatformActor(platform, tenant_id)
    sid, number = account_sid(), phone()
    connection = await harness.twilio(owner, sid, auth_token())
    assert connection.active_version is not None
    pinned = connection.active_version.id
    harness.verifier.numbers[sid] = [ProviderNumber("PN" + uuid4().hex, number)]
    await harness.service.bind_number(owner, connection.id, BindNumber(number=number))
    calls = CallService(harness.sessions)
    call = await calls.receive(
        number, "CA" + uuid4().hex, uuid4(), CallBinding(connection.id, pinned)
    )
    if revoked:
        connection = await harness.service.disconnect(
            owner, connection.id, RevisionCommand(revision=connection.revision)
        )
    else:
        # Erase pending credentials as well as the active generation.
        connection = await harness.service.submit_credentials(
            owner,
            connection.id,
            SubmitCredentials(revision=connection.revision, secrets={"auth_token": auth_token()}),
            str(uuid4()),
        )
    command = RevisionCommand(revision=connection.revision)
    with pytest.raises(AuthorizationError):
        await harness.service.use_platform(owner, connection.id, command)
    with pytest.raises(NotFoundError):
        await harness.service.use_platform(
            PlatformActor(platform, tenancy.b.tenant_id), connection.id, command
        )
    with pytest.raises(ConflictError):
        await harness.service.use_platform(
            actor, connection.id, RevisionCommand(revision=connection.revision - 1)
        )

    # Warm the shared resolver before resetting; this worker must not retain the key.
    harness.resolver._cache_seconds = 60  # pyright: ignore[reportPrivateUsage]
    await harness.resolver.active(tenant_id, "telephony")
    cleared = await harness.service.use_platform(actor, connection.id, command)
    assert cleared.status == "using_platform"
    assert cleared.account_identifier is None and cleared.config == {}
    assert cleared.last_checked_at is None and not cleared.has_secret
    assert cleared.active_version is None and cleared.pending_version is None
    assert cleared.revision == connection.revision + 1
    assert await harness.resolver.active(tenant_id, "telephony") is None
    assert await harness.resolver.for_call(tenant_id, connection.id, pinned) == []
    assert await calls.binding(tenant_id, call.call_id) == CallBinding(connection.id, pinned)
    replay = await harness.service.use_platform(actor, connection.id, command)
    assert replay == cleared
    async with tenancy.admin.begin() as session:
        versions = (
            await session.scalars(
                select(CredentialVersion).where(CredentialVersion.integration_id == connection.id)
            )
        ).all()
        assert versions and all(
            item.ciphertext is None and item.lease_token is None and item.lease_until is None
            for item in versions
        )
        old_phone = await session.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
        assert old_phone is not None and not old_phone.is_active
        # Historical calls and number bindings stay attributed to the old account.
        assert old_phone.integration_id == connection.id
        for model in (AuditEvent, PlatformAuditEvent):
            events = (
                await session.scalars(
                    select(model).where(
                        model.resource_id == connection.id,
                        model.action == "integration_platform_selected",
                    )
                )
            ).all()
            assert len(events) == 1 and events[0].tenant_id == tenant_id
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.resource_id == connection.id,
                AuditEvent.action == "integration_platform_selected",
            )
        )
        assert event is not None and event.platform_user_id == platform.user_id
        assert event.actor_id is None

    checks = {item.key: item for item in (await harness.readiness.readiness(owner)).checks}
    assert checks["telephony_connection"].status == "ready"
    assert checks["phone_number"].status == "blocking"
    # An assigned platform number has no clinic credential binding.
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenant_id, number=phone(), is_active=True))
    checks = {item.key: item for item in (await harness.readiness.readiness(owner)).checks}
    assert checks["phone_number"].status == "ready"


@pytest.mark.parametrize("configured", [False, True])
async def test_platform_readiness_and_reconnecting_own_speech_account(
    tenancy: TenancyFixture, configured: bool
) -> None:
    harness = Harness(
        tenancy,
        stt_provider="deepgram",
        deepgram_api_key=SecretStr("dg-platform" if configured else ""),
        tts_provider="elevenlabs",
        elevenlabs_api_key=SecretStr("el-platform" if configured else ""),
        llm_provider="gemini",
        gemini_api_key=SecretStr("gm-platform" if configured else ""),
    )
    owner = tenancy.context(tenancy.a)
    actor = PlatformActor(await IdentityHarness(tenancy).platform(), tenancy.a.tenant_id)
    await harness.voice(owner)
    embedding = await harness.connect(
        owner, "embeddings", "openrouter", {"api_key": "or-" + uuid4().hex}
    )
    for connection in await harness.service.list_all(owner):
        await harness.service.use_platform(
            actor, connection.id, RevisionCommand(revision=connection.revision)
        )
    checks = {item.key: item for item in (await harness.readiness.readiness(owner)).checks}
    expected = "ready" if configured else "blocking"
    assert all(
        checks[key].status == expected for key in ("speech_to_text", "text_to_speech", "agent")
    )
    assert ("knowledge" not in checks) == configured
    assert checks["telephony_connection"].status == "blocking"

    # Reuse the cleared slot, including providers with no configuration fields.
    cleared = await harness.service.get(owner, embedding.id)
    updated = await harness.service.update(
        owner, cleared.id, UpdateIntegration(revision=cleared.revision, config={})
    )
    assert updated.ownership == "byo"
    staged = await harness.service.submit_credentials(
        owner,
        updated.id,
        SubmitCredentials(revision=updated.revision, secrets={"api_key": "or-" + uuid4().hex}),
        str(uuid4()),
    )
    assert staged.pending_version is not None
    result = await harness.service.verify(
        owner,
        staged.id,
        VerifyCommand(revision=staged.revision, version_id=staged.pending_version.id),
    )
    assert result.activated and result.integration.ownership == "byo"
    with pytest.raises(ConflictError):
        await harness.service.use_platform(
            actor, cleared.id, RevisionCommand(revision=cleared.revision)
        )
    assert await harness.resolver.require(tenancy.a.tenant_id, "embeddings")


async def test_use_platform_http_requires_super_admin(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    connection = await harness.connect(
        tenancy.context(tenancy.a), "stt", "deepgram", {"api_key": "dg-" + uuid4().hex}
    )
    admin = await IdentityHarness(tenancy).admin()
    app = create_app(harness.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    path = (
        f"/api/v1/platform/tenants/{tenancy.a.tenant_id}/integrations/{connection.id}/use-platform"
    )
    body = {"revision": connection.revision}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.post(path, json=body)).status_code == 401
        assert (await client.post(path, json=body, headers=tenancy.a.headers)).status_code == 403
        headers = {"Authorization": f"Bearer {admin.token.get_secret_value()}"}
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["status"] == "using_platform"
        assert response.json()["has_secret"] is False
