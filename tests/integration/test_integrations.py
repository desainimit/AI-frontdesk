"""Tenant provider connections: isolation, write-only secrets, rotation and call routing."""

import base64
import json
import secrets
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update
from twilio.request_validator import RequestValidator  # pyright: ignore[reportMissingTypeStubs]

from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext, Role
from app.calls.models import Call, PhoneNumber
from app.calls.service import CallBinding, CallService
from app.config.settings import Settings
from app.database.rls import set_tenant_scope
from app.errors import AuthorizationError, ConflictError, NotFoundError
from app.events.idempotency import IdempotencyConflictError
from app.integrations.catalog import Purpose
from app.integrations.models import CredentialVersion, TenantIntegration
from app.integrations.resolver import IntegrationUnavailableError, TenantIntegrationResolver
from app.integrations.service import (
    Actor,
    BindNumber,
    CreateIntegration,
    IntegrationService,
    IntegrationValidationError,
    IntegrationView,
    NumberNotOwnedError,
    PlatformActor,
    RevisionCommand,
    SubmitCredentials,
    UpdateIntegration,
    VerifyCommand,
    VersionCommand,
)
from app.integrations.verifiers import ProviderNumber, Verification
from app.main import create_app
from app.platform.bootstrap import seed_super_admin
from app.platform.models import PlatformAuditEvent
from app.platform.service import PlatformAuthenticationService
from app.platform.support import EnterSupportCommand, SupportAccessService
from app.tenants.models import Tenant
from app.tenants.readiness import NotReadyError, TenantReadinessService
from app.users.models import User
from tests.fakes import FakeDependency
from tests.integration.platform_mfa_helpers import mfa_login
from tests.integration.test_platform import seed_command
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


def integration_settings(base: Settings, **overrides: object) -> Settings:
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    return base.model_copy(
        update={
            "tenant_integrations_enabled": True,
            "secret_encryption_keys": SecretStr(json.dumps({"k1": key})),
            "secret_encryption_active_key": "k1",
            "stream_token_key": SecretStr(secrets.token_urlsafe(48)),
            "integration_cache_seconds": 0,
            "telephony_enabled": True,
            "telephony_mode": "echo",
            "telephony_public_base_url": "https://voice.example.test",
            "twilio_account_sid": "",
            "twilio_auth_token": SecretStr(""),
            "agent_enabled": False,
            "knowledge_enabled": False,
            **overrides,
        }
    )


class FakeVerifier:
    """Stands in for the provider; records what it was asked without any network."""

    def __init__(self) -> None:
        self.outcome = Verification("verified", "ok")
        self.numbers: dict[str, list[ProviderNumber]] = {}
        self.seen: list[dict[str, str]] = []

    async def verify(
        self,
        purpose: str,
        provider: str,
        account_identifier: str | None,
        config: Mapping[str, Any],
        secrets: Mapping[str, str],
    ) -> Verification:
        self.seen.append(dict(secrets))
        return self.outcome

    async def twilio_numbers(
        self, account_sid: str, secrets: Mapping[str, str], number: str | None = None
    ) -> list[ProviderNumber]:
        owned = self.numbers.get(account_sid, [])
        return [item for item in owned if number is None or item.number == number]


def account_sid() -> str:
    return "AC" + uuid4().hex


def auth_token() -> str:
    return uuid4().hex


def phone() -> str:
    return "+1" + str(uuid4().int)[:10]


class Harness:
    def __init__(self, tenancy: TenancyFixture, **overrides: object) -> None:
        self.tenancy = tenancy
        self.settings = integration_settings(tenancy.settings, **overrides)
        self.sessions = tenancy.database.session_factory
        self.verifier = FakeVerifier()
        self.resolver = TenantIntegrationResolver(self.sessions, self.settings)
        self.service = IntegrationService(
            self.sessions, self.settings, verifier=self.verifier, resolver=self.resolver
        )
        self.readiness = TenantReadinessService(self.sessions, self.settings, self.resolver)

    async def connect(
        self, actor: Actor, purpose: str, provider: str, secrets: dict[str, str], **extra: Any
    ) -> IntegrationView:
        """Create, submit, verify and activate one connection."""
        view = await self.service.create(
            actor, CreateIntegration(purpose=Purpose(purpose), provider=provider, **extra)
        )
        view = await self.service.submit_credentials(
            actor, view.id, SubmitCredentials(revision=view.revision, secrets=secrets), str(uuid4())
        )
        assert view.pending_version is not None
        result = await self.service.verify(
            actor,
            view.id,
            VerifyCommand(revision=view.revision, version_id=view.pending_version.id),
        )
        assert result.outcome == "verified" and result.activated
        return result.integration

    async def twilio(self, actor: Actor, sid: str, token: str) -> IntegrationView:
        return await self.connect(
            actor, "telephony", "twilio", {"auth_token": token}, account_identifier=sid
        )

    async def voice(self, actor: Actor) -> None:
        await self.connect(actor, "stt", "deepgram", {"api_key": "dg-" + uuid4().hex})
        await self.connect(actor, "tts", "elevenlabs", {"api_key": "el-" + uuid4().hex})


async def test_connections_are_isolated_and_secrets_are_write_only(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    a, b = tenancy.context(tenancy.a), tenancy.context(tenancy.b)
    sid, token = account_sid(), auth_token()
    created = await harness.service.create(
        a, CreateIntegration(purpose=Purpose.TELEPHONY, provider="twilio", account_identifier=sid)
    )
    assert created.status == "not_configured" and not created.has_secret
    with pytest.raises(ConflictError):  # One connection per purpose and slot.
        await harness.service.create(
            a,
            CreateIntegration(purpose=Purpose.TELEPHONY, provider="twilio", account_identifier=sid),
        )

    key = str(uuid4())
    command = SubmitCredentials(revision=created.revision, secrets={"auth_token": token})
    staged = await harness.service.submit_credentials(a, created.id, command, key)
    replay = await harness.service.submit_credentials(a, created.id, command, key)
    assert replay == staged and staged.status == "saved_unverified"
    assert staged.pending_version is not None
    assert staged.pending_version.display_suffix == token[-4:]
    with pytest.raises(IdempotencyConflictError):
        await harness.service.submit_credentials(
            a,
            created.id,
            SubmitCredentials(revision=created.revision, secrets={"auth_token": auth_token()}),
            key,
        )
    # Nothing a client can read back contains the secret.
    assert token not in staged.model_dump_json()
    assert token not in repr(command)
    with pytest.raises(IntegrationValidationError):
        await harness.service.submit_credentials(
            a,
            created.id,
            SubmitCredentials(revision=staged.revision, secrets={"auth_token": "short"}),
            str(uuid4()),
        )

    # Tenant B cannot see, change or use tenant A's connection, version or operation.
    version_id = staged.pending_version.id
    assert await harness.service.list_all(b) == []
    for operation in (
        harness.service.get(b, created.id),
        harness.service.update(b, created.id, UpdateIntegration(revision=2, config={})),
        harness.service.submit_credentials(
            b, created.id, SubmitCredentials(revision=2, secrets={"auth_token": token}), "k"
        ),
        harness.service.verify(
            b, created.id, VerifyCommand(revision=staged.revision, version_id=version_id)
        ),
        harness.service.activate(
            b, created.id, VersionCommand(revision=staged.revision, version_id=version_id)
        ),
        harness.service.disconnect(b, created.id, RevisionCommand(revision=staged.revision)),
    ):
        with pytest.raises(NotFoundError):
            await operation
    async with harness.sessions.begin() as session:
        await set_tenant_scope(session, tenancy.b.tenant_id)
        # RLS itself, with no WHERE clause.
        assert (await session.scalars(select(TenantIntegration))).all() == []
        assert (await session.scalars(select(CredentialVersion))).all() == []
    async with harness.sessions.begin() as session:
        assert (await session.scalars(select(CredentialVersion))).all() == []  # No scope.

    verified = await harness.service.verify(
        a, created.id, VerifyCommand(revision=staged.revision, version_id=version_id)
    )
    assert verified.outcome == "verified" and verified.activated
    assert verified.integration.status == "verified" and verified.integration.has_secret
    assert harness.verifier.seen == [{"auth_token": token}]
    resolved = await harness.resolver.require(tenancy.a.tenant_id, "telephony")
    assert resolved.secret("auth_token").get_secret_value() == token
    assert token not in repr(resolved)
    assert await harness.resolver.active(tenancy.b.tenant_id, "telephony") is None
    with pytest.raises(IntegrationUnavailableError):
        await harness.resolver.require(tenancy.b.tenant_id, "telephony")

    async with tenancy.admin.begin() as session:
        row = await session.get(CredentialVersion, version_id)
        assert row is not None and row.ciphertext is not None
        assert token.encode() not in row.ciphertext and token not in row.fingerprint
        actions = set(
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.resource_id == created.id)
            )
        )
        assert {
            "integration_created",
            "integration_credential_staged",
            "integration_verification_verified",
        } <= actions
        # Ciphertext moved to another tenant's row must not decrypt there.
        other = await harness.twilio(b, account_sid(), auth_token())
        assert other.active_version is not None
        await session.execute(
            update(CredentialVersion)
            .where(CredentialVersion.id == other.active_version.id)
            .values(ciphertext=row.ciphertext)
        )
    with pytest.raises(IntegrationUnavailableError):
        await harness.resolver.active(tenancy.b.tenant_id, "telephony")
    assert (
        await harness.resolver.require(tenancy.a.tenant_id, "telephony")
    ).account_identifier == sid


async def test_rotation_keeps_the_working_version_until_a_replacement_verifies(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    a = tenancy.context(tenancy.a)
    tenant_id = tenancy.a.tenant_id
    sid, old = account_sid(), auth_token()
    live = await harness.twilio(a, sid, old)
    assert live.active_version is not None
    old_version = live.active_version.id

    async def submit(token: str, revision: int) -> IntegrationView:
        return await harness.service.submit_credentials(
            a,
            live.id,
            SubmitCredentials(revision=revision, secrets={"auth_token": token}),
            str(uuid4()),
        )

    # 1. Provider unreachable: nothing changes and the attempt can be repeated.
    staged = await submit(auth_token(), live.revision)
    assert staged.pending_version is not None and staged.status == "verified"
    harness.verifier.outcome = Verification("unavailable", "provider_unavailable")
    result = await harness.service.verify(
        a, live.id, VerifyCommand(revision=staged.revision, version_id=staged.pending_version.id)
    )
    assert result.outcome == "unavailable" and not result.activated
    assert result.integration.pending_version is not None
    assert result.integration.pending_version.state == "staged"

    # 2. Provider rejects the replacement: the working credential stays active.
    harness.verifier.outcome = Verification("invalid", "credentials_rejected")
    rejected = await harness.service.verify(
        a,
        live.id,
        VerifyCommand(revision=result.integration.revision, version_id=staged.pending_version.id),
    )
    assert rejected.outcome == "invalid" and rejected.integration.status == "verified"
    assert rejected.integration.active_version is not None
    assert rejected.integration.active_version.id == old_version
    assert rejected.integration.pending_version is not None
    assert rejected.integration.pending_version.error_code == "credentials_rejected"
    current = await harness.resolver.require(tenant_id, "telephony")
    assert current.secret("auth_token").get_secret_value() == old
    with pytest.raises(ConflictError):  # A rejected version can never be promoted.
        await harness.service.activate(
            a,
            live.id,
            VersionCommand(
                revision=rejected.integration.revision, version_id=staged.pending_version.id
            ),
        )

    # 3. Verify without activating, then promote explicitly.
    new = auth_token()
    harness.verifier.outcome = Verification("verified", "ok")
    staged = await submit(new, rejected.integration.revision)
    assert staged.pending_version is not None
    with pytest.raises(ConflictError):  # Stale revision.
        await harness.service.verify(
            a, live.id, VerifyCommand(revision=1, version_id=staged.pending_version.id)
        )
    checked = await harness.service.verify(
        a,
        live.id,
        VerifyCommand(
            revision=staged.revision, version_id=staged.pending_version.id, activate=False
        ),
    )
    assert not checked.activated
    still = await harness.resolver.require(tenant_id, "telephony")
    assert still.secret("auth_token").get_secret_value() == old
    promote = VersionCommand(
        revision=checked.integration.revision, version_id=staged.pending_version.id
    )
    promoted = await harness.service.activate(a, live.id, promote)
    assert await harness.service.activate(a, live.id, promote) == promoted  # Safe replay.
    assert promoted.active_version is not None
    assert promoted.active_version.id == staged.pending_version.id
    rotated = await harness.resolver.require(tenant_id, "telephony")
    assert rotated.secret("auth_token").get_secret_value() == new

    # A call pinned to the old version accepts the current secret first, then its own.
    pinned = await harness.resolver.for_call(tenant_id, live.id, old_version)
    assert [item.secret("auth_token").get_secret_value() for item in pinned] == [new, old]

    # 4. Disconnect erases every stored secret and stops routing its numbers.
    number = phone()
    harness.verifier.numbers[sid] = [ProviderNumber("PN" + uuid4().hex, number)]
    bound = await harness.service.bind_number(a, live.id, BindNumber(number=number))
    assert bound.is_active and bound.voice_webhook_url.endswith("/webhooks/v1/twilio/incoming")
    gone = await harness.service.disconnect(a, live.id, RevisionCommand(revision=promoted.revision))
    assert gone.status == "revoked" and not gone.has_secret
    assert await harness.resolver.active(tenant_id, "telephony") is None
    assert await harness.resolver.for_call(tenant_id, live.id, old_version) == []
    async with tenancy.admin.begin() as session:
        rows = (
            await session.scalars(
                select(CredentialVersion).where(CredentialVersion.integration_id == live.id)
            )
        ).all()
        assert rows and all(row.ciphertext is None for row in rows)
        routed = await session.get(PhoneNumber, bound.id)
        assert routed is not None and not routed.is_active


async def test_only_tenant_admins_and_explicit_platform_actions_manage_connections(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    tenant_id = tenancy.a.tenant_id
    admin = tenancy.context(tenancy.a)
    member_id = uuid4()
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenant_id)
        session.add(
            User(
                id=member_id,
                tenant_id=tenant_id,
                email=f"{uuid4()}@example.test",
                display_name="Member",
                role="member",
            )
        )
    member = RequestContext(uuid4(), tenant_id, member_id, ActorType.USER, Role.MEMBER)
    seed = seed_command()
    await seed_super_admin(tenancy.admin, seed)
    platform_auth = PlatformAuthenticationService(harness.sessions)
    login = await mfa_login(platform_auth, seed, tenancy.settings)
    platform = await platform_auth.authenticate(login.access_token, uuid4())
    support_access = SupportAccessService(harness.sessions)
    entered = await support_access.enter(
        platform,
        EnterSupportCommand.model_validate(
            {
                "tenant_id": tenant_id,
                "reason": "Check that support cannot manage credentials",
                "capabilities": ["tenant:read", "tenant:update"],
                "confirm_sensitive": True,
            }
        ),
    )
    support = await support_access.authenticate(entered.access_token, uuid4())

    create = CreateIntegration(purpose=Purpose.STT, provider="deepgram")
    for actor in (member, support):
        with pytest.raises(AuthorizationError):
            await harness.service.list_all(actor)
        with pytest.raises(AuthorizationError):
            await harness.service.create(actor, create)
        with pytest.raises(AuthorizationError):
            await harness.readiness.set_live(actor, True)

    # The platform acts on exactly the tenant it names, and both audit trails record it.
    acting = PlatformActor(platform, tenant_id)
    view = await harness.connect(acting, "stt", "deepgram", {"api_key": "dg-" + uuid4().hex})
    assert view.tenant_id == tenant_id and view.status == "verified"
    assert [item.id for item in await harness.service.list_all(admin)] == [view.id]
    assert await harness.service.list_all(PlatformActor(platform, tenancy.b.tenant_id)) == []
    with pytest.raises(NotFoundError):
        await harness.service.list_all(PlatformActor(platform, uuid4()))
    async with tenancy.admin.begin() as session:
        tenant_audit = (
            await session.scalars(select(AuditEvent).where(AuditEvent.resource_id == view.id))
        ).all()
        assert tenant_audit and all(
            row.platform_user_id == platform.user_id and row.tenant_id == tenant_id
            for row in tenant_audit
        )
        assert await session.scalar(
            select(PlatformAuditEvent.id).where(PlatformAuditEvent.resource_id == view.id).limit(1)
        )

    # Only allowlisted configuration is accepted; endpoint-selecting fields are locked.
    for config in ({"model": "nova-3", "base_url": "https://evil.example"}, {"model": "x"}):
        with pytest.raises(IntegrationValidationError):
            await harness.service.update(
                admin, view.id, UpdateIntegration(revision=view.revision, config=config)
            )
    updated = await harness.service.update(
        admin,
        view.id,
        UpdateIntegration(revision=view.revision, config={"model": "nova-3", "language": "en"}),
    )
    assert updated.config == {"model": "nova-3", "language": "en"} and updated.has_secret
    azure = await harness.connect(
        admin,
        "tts",
        "azure_speech",
        {"speech_key": uuid4().hex},
        config={"region": "eastus"},
    )
    with pytest.raises(ConflictError):
        await harness.service.update(
            admin,
            azure.id,
            UpdateIntegration(revision=azure.revision, config={"region": "westeurope"}),
        )
    with pytest.raises(IntegrationValidationError):
        await harness.service.create(
            admin,
            CreateIntegration(
                purpose=Purpose.LLM, provider="gemini", config={"model": "not-allowed"}
            ),
        )
    with pytest.raises(IntegrationValidationError):
        await harness.service.create(
            admin, CreateIntegration(purpose=Purpose.STT, provider="twilio")
        )


async def test_readiness_activation_and_number_ownership(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    a, b = tenancy.context(tenancy.a), tenancy.context(tenancy.b)
    tenant_id = tenancy.a.tenant_id
    calls = CallService(harness.sessions)
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(service_state="setup")
        )

    initial = await harness.readiness.readiness(a)
    blocking = {check.key for check in initial.checks if check.status == "blocking"}
    assert not initial.ready and initial.service_state == "setup"
    assert blocking == {
        "telephony_connection",
        "phone_number",
        "speech_to_text",
        "text_to_speech",
    }
    with pytest.raises(NotReadyError):
        await harness.readiness.set_live(a, True)

    sid, token, number = account_sid(), auth_token(), phone()
    telephony = await harness.twilio(a, sid, token)
    await harness.voice(a)
    assert telephony.active_version is not None
    binding = CallBinding(telephony.id, telephony.active_version.id)

    # Ownership comes from the provider: an arbitrary pasted number is refused.
    with pytest.raises(NumberNotOwnedError):
        await harness.service.bind_number(a, telephony.id, BindNumber(number=number))
    harness.verifier.numbers[sid] = [ProviderNumber("PN" + uuid4().hex, number)]
    listed = await harness.service.provider_numbers(a, telephony.id)
    assert [(item.number, item.bound) for item in listed] == [(number, False)]
    bound = await harness.service.bind_number(a, telephony.id, BindNumber(number=number))
    assert bound.integration_id == telephony.id
    assert (await harness.service.bind_number(a, telephony.id, BindNumber(number=number))) == bound

    # Another tenant cannot claim a number already routed, even if its account lists it.
    other_sid = account_sid()
    other = await harness.twilio(b, other_sid, auth_token())
    harness.verifier.numbers[other_sid] = [ProviderNumber("PN" + uuid4().hex, number)]
    with pytest.raises(ConflictError):
        await harness.service.bind_number(b, other.id, BindNumber(number=number))

    # Setup workspaces take no calls, however complete their configuration.
    with pytest.raises(AuthorizationError):
        await calls.receive(number, "CA" + uuid4().hex, uuid4(), binding)
    ready = await harness.readiness.readiness(a)
    assert ready.ready and all(check.status != "blocking" for check in ready.checks)
    live = await harness.readiness.set_live(a, True)
    assert live.service_state == "live"

    call = await calls.receive(number, "CA" + uuid4().hex, uuid4(), binding)
    assert call.tenant_id == tenant_id
    assert await calls.binding(tenant_id, call.call_id) == binding
    assert await calls.binding(tenancy.b.tenant_id, call.call_id) is None
    # The number is served only by the connection it was proven against.
    assert other.active_version is not None
    for forged in (
        CallBinding(None, None),
        CallBinding(other.id, other.active_version.id),
    ):
        with pytest.raises(AuthorizationError):
            await calls.receive(number, "CA" + uuid4().hex, uuid4(), forged)
    # A status callback authenticated by a different account cannot end this call.
    with pytest.raises(NotFoundError):
        await calls.provider_status(number, call.provider_call_id, "completed", uuid4(), other.id)

    # Losing a required connection blocks reactivation; deactivation stops new calls.
    stt = next(item for item in await harness.service.list_all(a) if item.purpose == "stt")
    await harness.service.disconnect(a, stt.id, RevisionCommand(revision=stt.revision))
    assert (await harness.readiness.set_live(a, False)).service_state == "setup"
    with pytest.raises(NotReadyError):
        await harness.readiness.set_live(a, True)
    with pytest.raises(AuthorizationError):
        await calls.receive(number, "CA" + uuid4().hex, uuid4(), binding)
    async with tenancy.admin.begin() as session:
        actions = set(
            await session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.tenant_id == tenant_id, AuditEvent.resource_type == "tenant"
                )
            )
        )
        assert {"tenant_service_activated", "tenant_service_deactivated"} <= actions


async def test_two_twilio_accounts_route_to_two_tenants_over_http(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    settings = harness.settings
    accounts: dict[UUID, tuple[str, str, str]] = {}
    for tenant in (tenancy.a, tenancy.b):
        actor = tenancy.context(tenant)
        sid, token, number = account_sid(), auth_token(), phone()
        harness.verifier.numbers[sid] = [ProviderNumber("PN" + uuid4().hex, number)]
        telephony = await harness.twilio(actor, sid, token)
        await harness.voice(actor)
        await harness.service.bind_number(actor, telephony.id, BindNumber(number=number))
        accounts[tenant.tenant_id] = (sid, token, number)
    sid_a, token_a, number_a = accounts[tenancy.a.tenant_id]
    sid_b, token_b, number_b = accounts[tenancy.b.tenant_id]

    app = create_app(settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    path = "/webhooks/v1/twilio/incoming"

    def signed(token: str, params: dict[str, str], route: str = path) -> dict[str, str]:
        url = settings.telephony_public_base_url + route
        signature = RequestValidator(token).compute_signature(url, params)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return {"X-Twilio-Signature": str(signature)}  # pyright: ignore[reportUnknownArgumentType]

    def call(sid: str, number: str) -> dict[str, str]:
        return {"AccountSid": sid, "CallSid": "CA" + uuid4().hex, "To": number}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        app.state.integration_verifier = harness.verifier
        for sid, token, number, tenant in (
            (sid_a, token_a, number_a, tenancy.a),
            (sid_b, token_b, number_b, tenancy.b),
        ):
            params = call(sid, number)
            response = await client.post(path, data=params, headers=signed(token, params))
            assert response.status_code == 200 and str(tenant.tenant_id) in response.text
            assert token not in response.text  # The stream token is not a provider secret.
            async with tenancy.admin.begin() as session:
                row = (
                    await session.scalars(
                        select(Call).where(Call.provider_call_id == params["CallSid"])
                    )
                ).one()
                assert row.tenant_id == tenant.tenant_id and row.integration_id is not None

        # Tenant B's valid credentials cannot open a call on tenant A's number...
        forged = call(sid_b, number_a)
        response = await client.post(path, data=forged, headers=signed(token_b, forged))
        assert response.status_code == 403
        # ...nor can a correct signature paired with another account's identifier.
        forged = call(sid_b, number_a)
        response = await client.post(path, data=forged, headers=signed(token_a, forged))
        assert response.status_code == 403
        # There is no platform account to fall back to for an unknown destination.
        unknown = call(sid_a, phone())
        response = await client.post(path, data=unknown, headers=signed(token_a, unknown))
        assert response.status_code == 403
        async with tenancy.admin.begin() as session:
            for params in (forged, unknown):
                assert (
                    await session.scalar(
                        select(Call.id).where(Call.provider_call_id == params["CallSid"])
                    )
                    is None
                )

        # Later events are bound to the call's own account, not to the path alone.
        params = call(sid_a, number_a)
        await client.post(path, data=params, headers=signed(token_a, params))
        async with tenancy.admin.begin() as session:
            row = (
                await session.scalars(
                    select(Call).where(Call.provider_call_id == params["CallSid"])
                )
            ).one()
        route = f"/webhooks/v1/twilio/{row.tenant_id}/{row.id}/status"
        event = {
            "AccountSid": sid_b,
            "CallSid": params["CallSid"],
            "StreamEvent": "stream-stopped",
        }
        response = await client.post(route, data=event, headers=signed(token_b, event, route))
        assert response.status_code == 403
        event["AccountSid"] = sid_a
        response = await client.post(route, data=event, headers=signed(token_a, event, route))
        assert response.status_code == 204

        # The management API never returns a stored secret and refuses other tenants.
        headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
        listing = await client.get("/api/v1/integrations", headers=headers)
        assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
        assert len(listing.json()) == 3
        for secret in (token_a, token_b):
            assert secret not in listing.text
        own = next(item["id"] for item in listing.json() if item["purpose"] == "telephony")
        other = {"Authorization": f"Bearer {tenancy.b.token.get_secret_value()}"}
        assert (await client.get(f"/api/v1/integrations/{own}", headers=other)).status_code == 404
        rejected = await client.post(
            f"/api/v1/integrations/{own}/credential-versions",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"revision": 1, "secrets": {"auth_token": "leaked-" + token_a}},
        )
        assert rejected.status_code in {409, 422} and token_a not in rejected.text
        catalog = await client.get("/api/v1/integration-providers", headers=headers)
        assert catalog.status_code == 200
        assert {item["provider"] for item in catalog.json()} == {
            "twilio",
            "deepgram",
            "azure_speech",
            "elevenlabs",
            "gemini",
            "openrouter",
        }
        readiness = await client.get("/api/v1/tenants/current/readiness", headers=headers)
        assert readiness.status_code == 200 and readiness.json()["ready"] is True


async def test_missing_connection_never_falls_back_to_platform_or_other_tenants(
    tenancy: TenancyFixture,
) -> None:
    platform_token = auth_token()
    harness = Harness(
        tenancy,
        twilio_account_sid=account_sid(),
        twilio_auth_token=SecretStr(platform_token),
        legacy_provider_tenant_ids=str(tenancy.b.tenant_id),
    )
    from app.api.telephony import resolve_call_providers, tenant_candidates

    app = create_app(harness.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with app.router.lifespan_context(app):
        # Tenant A has no connection and is not allowlisted: no account may sign for it.
        assert await tenant_candidates(app, tenancy.a.tenant_id) == []
        with pytest.raises(IntegrationUnavailableError):
            await resolve_call_providers(app, harness.settings, tenancy.a.tenant_id)
        # Tenant B is explicitly approved to keep the platform account.
        legacy = await tenant_candidates(app, tenancy.b.tenant_id)
        assert [item.integration_id for item in legacy] == [None]
        assert legacy[0].auth_token.get_secret_value() == platform_token
        # Once A connects its own account, only that account is a candidate for A.
        sid, token = account_sid(), auth_token()
        await harness.twilio(tenancy.context(tenancy.a), sid, token)
        own = await tenant_candidates(app, tenancy.a.tenant_id, fresh=True)
        assert [(item.account_sid, item.auth_token.get_secret_value()) for item in own] == [
            (sid, token)
        ]


@pytest.mark.parametrize("case", ["own", "other_account", "platform_token", "foreign_token"])
async def test_media_stream_is_bound_to_the_calls_own_connection(
    tenancy: TenancyFixture, case: str
) -> None:
    import asyncio

    from fastapi import FastAPI
    from starlette.types import Message
    from starlette.websockets import WebSocket

    from app.api.telephony import call_candidates, run_media
    from app.voice.twilio import TwilioCredentials, stream_token

    harness = Harness(tenancy)
    settings = harness.settings
    sid, token, number = account_sid(), auth_token(), phone()
    harness.verifier.numbers[sid] = [ProviderNumber("PN" + uuid4().hex, number)]
    actor = tenancy.context(tenancy.a)
    telephony = await harness.twilio(actor, sid, token)
    await harness.service.bind_number(actor, telephony.id, BindNumber(number=number))
    other_sid, other_token = account_sid(), auth_token()
    other = await harness.twilio(tenancy.context(tenancy.b), other_sid, other_token)
    assert telephony.active_version is not None and other.active_version is not None

    app = FastAPI()
    app.state.settings = settings
    app.state.integration_resolver = harness.resolver
    service = CallService(harness.sessions)
    context = await service.receive(
        number,
        "CA" + uuid4().hex,
        uuid4(),
        CallBinding(telephony.id, telephony.active_version.id),
    )
    # Only the call's own account is a candidate, whichever tenant the path names.
    candidates = await call_candidates(app, service, context.tenant_id, context.call_id)
    assert [(item.account_sid, item.integration_id) for item in candidates] == [(sid, telephony.id)]
    assert await call_candidates(app, service, tenancy.b.tenant_id, context.call_id) == []
    credentials = candidates[0]
    foreign = TwilioCredentials(
        other_sid,
        SecretStr(other_token),
        integration_id=other.id,
        version_id=other.active_version.id,
    )
    presented = {
        "own": stream_token(settings, context, credentials),
        "other_account": stream_token(settings, context, credentials),
        "platform_token": stream_token(settings, context),
        "foreign_token": stream_token(settings, context, foreign),
    }[case]

    stream_sid = "MZ" + uuid4().hex
    inbound: asyncio.Queue[Message] = asyncio.Queue()
    outbound: asyncio.Queue[Message] = asyncio.Queue()
    socket = WebSocket({"type": "websocket", "path": "/", "app": app}, inbound.get, outbound.put)
    await inbound.put({"type": "websocket.connect"})
    await socket.accept()
    await outbound.get()

    async def send(value: dict[str, Any]) -> None:
        await inbound.put({"type": "websocket.receive", "text": json.dumps(value)})

    await send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    await send(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "accountSid": other_sid if case == "other_account" else sid,
                "callSid": context.provider_call_id,
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"token": presented},
            },
        }
    )
    if case == "own":
        await send(
            {
                "event": "stop",
                "sequenceNumber": "2",
                "streamSid": stream_sid,
                "stop": {"accountSid": sid, "callSid": context.provider_call_id},
            }
        )
    async with asyncio.timeout(15):
        await run_media(socket, service, settings, context.tenant_id, context.call_id, credentials)
    row = (await service.list(actor, 1, 0, context.call_id))[0]
    if case == "own":
        assert row.status == "completed" and row.end_reason == "stream_stopped"
    else:
        assert row.status == "received" and row.started_at is None


async def test_provider_or_account_can_change_only_while_no_secret_is_stored(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    a = tenancy.context(tenancy.a)
    stt = await harness.connect(a, "stt", "deepgram", {"api_key": "dg-" + uuid4().hex})
    switch = {"provider": "azure_speech", "config": {"region": "westeurope"}}
    with pytest.raises(ConflictError):
        await harness.service.update(
            a, stt.id, UpdateIntegration.model_validate({"revision": stt.revision, **switch})
        )
    gone = await harness.service.disconnect(a, stt.id, RevisionCommand(revision=stt.revision))
    moved = await harness.service.update(
        a, stt.id, UpdateIntegration.model_validate({"revision": gone.revision, **switch})
    )
    assert (moved.provider, moved.status, moved.has_secret) == (
        "azure_speech",
        "not_configured",
        False,
    )
    assert moved.config["region"] == "westeurope"
    again = await harness.service.submit_credentials(
        a,
        stt.id,
        SubmitCredentials(revision=moved.revision, secrets={"speech_key": "az-" + uuid4().hex}),
        str(uuid4()),
    )
    assert again.pending_version is not None and again.status == "saved_unverified"

    phone_service = await harness.service.create(
        a,
        CreateIntegration(
            purpose=Purpose.TELEPHONY, provider="twilio", account_identifier=account_sid()
        ),
    )
    corrected = account_sid()
    fixed = await harness.service.update(
        a,
        phone_service.id,
        UpdateIntegration(revision=phone_service.revision, config={}, account_identifier=corrected),
    )
    assert fixed.account_identifier == corrected
    with pytest.raises(IntegrationValidationError):
        await harness.service.update(
            a,
            phone_service.id,
            UpdateIntegration(revision=fixed.revision, config={}, account_identifier="nope"),
        )
