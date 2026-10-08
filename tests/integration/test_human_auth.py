"""Invitation, password login, OTP recovery and team management on a real database."""

import base64
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.audit.models import AuditEvent
from app.auth.context import RequestContext
from app.auth.human import HumanAuthenticationService
from app.auth.human_models import AuthChallenge, AuthMail, HumanAccount, HumanIdentity, HumanSession
from app.auth.human_schemas import (
    AcceptInvitation,
    EmailIdentity,
    HumanLogin,
    InviteMember,
    ResetPassword,
    TeamMemberUpdate,
)
from app.auth.mail_worker import deliver_one
from app.auth.service import AuthenticationService
from app.config.settings import Settings
from app.database.rls import set_tenant_scope
from app.database.session import Database
from app.database.unit_of_work import UnitOfWork
from app.errors import AuthenticationError, AuthorizationError, ConflictError, NotFoundError
from app.platform.bootstrap import seed_super_admin
from app.platform.onboarding import OnboardingDraft, OnboardingView, TenantOnboardingService
from app.platform.policies import PlatformContext
from app.platform.service import PlatformAuthenticationService
from app.platform.support import EnterSupportCommand, SupportAccessService
from app.security.encryption import SecretCipher
from app.tenants.schemas import TenantName
from app.tenants.service import TenantService
from app.users.models import User
from app.users.service import UserService
from tests.integration.platform_mfa_helpers import mfa_login
from tests.integration.test_platform import seed_command
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

PASSWORD = "correct horse battery staple 9"


def auth_settings(base: Settings) -> Settings:
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    return base.model_copy(
        update={
            "tenant_onboarding_enabled": True,
            "tenant_human_auth_enabled": True,
            "tenant_auth_public_origin": "https://app.example.test",
            "secret_encryption_keys": SecretStr(json.dumps({"k1": key})),
            "secret_encryption_active_key": "k1",
            "otp_hmac_key": SecretStr(secrets.token_urlsafe(48)),
            "smtp_enabled": True,
            "smtp_host": "smtp.example.test",
            "smtp_sender": "noreply@example.test",
        }
    )


class Harness:
    def __init__(self, tenancy: TenancyFixture) -> None:
        self.tenancy = tenancy
        self.settings = auth_settings(tenancy.settings)
        self.sessions = tenancy.database.session_factory
        self.auth = HumanAuthenticationService(self.sessions, self.settings)
        self.cipher = SecretCipher(self.settings.secret_encryption_keys, "k1")

    async def platform(self) -> PlatformContext:
        seed = seed_command()
        await seed_super_admin(self.tenancy.admin, seed)
        service = PlatformAuthenticationService(self.sessions)
        login = await mfa_login(service, seed, self.settings)
        return await service.authenticate(login.access_token, uuid4())

    async def onboard(self, context: PlatformContext, email: str) -> OnboardingView:
        draft = OnboardingDraft.model_validate(
            {
                "name": f"Clinic {uuid4()}",
                "administrator": {"email": email, "display_name": "Clinic Admin"},
                "location": {"name": "Main", "timezone": "Asia/Kolkata"},
                "language_policy": {"default_language": "en", "enabled_languages": ["en"]},
            }
        )
        return await TenantOnboardingService(self.sessions).create(context, draft, str(uuid4()))

    async def mail(self, tenant_id: UUID, user_id: UUID) -> dict[str, str]:
        """Read the queued message the way the delivery worker would."""
        async with self.tenancy.admin.begin() as session:
            row = (
                await session.execute(
                    select(AuthMail)
                    .join(AuthChallenge, AuthChallenge.id == AuthMail.challenge_id)
                    .where(AuthChallenge.user_id == user_id, AuthMail.ciphertext.is_not(None))
                    .order_by(AuthMail.created_at.desc())
                )
            ).scalar_one()
            assert row.ciphertext is not None
            return json.loads(
                self.cipher.decrypt(row.key_id, row.ciphertext, f"auth-mail:{tenant_id}:{row.id}")
            )

    async def token(self, tenant_id: UUID, user_id: UUID) -> str:
        body = (await self.mail(tenant_id, user_id))["body"]
        match = re.search(r"#token=(\S+)", body)
        assert match is not None
        return match.group(1)

    async def accept(self, token: str, password: str = PASSWORD) -> None:
        await self.auth.accept(
            AcceptInvitation(token=SecretStr(token), password=SecretStr(password)), uuid4()
        )

    async def login(self, tenant_id: UUID, email: str, password: str = PASSWORD) -> RequestContext:
        view = await self.auth.login(
            HumanLogin(tenant_id=tenant_id, email=email, password=SecretStr(password)), uuid4()
        )
        assert view.access_token.startswith("fhu1.") and view.tenant_id == tenant_id
        return await AuthenticationService(self.sessions).authenticate(view.access_token, uuid4())


async def test_invitation_login_and_two_tenant_isolation(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    platform = await harness.platform()
    email = f"{uuid4()}@example.test"
    first = await harness.onboard(platform, email)
    second = await harness.onboard(platform, email)  # Same email, separate memberships.
    assert first.invitation_status == "not_sent" and first.service_state == "setup"

    key = str(uuid4())
    invite = await harness.auth.invite_initial_admin(platform, first.tenant_id, key)
    replay = await harness.auth.invite_initial_admin(platform, first.tenant_id, key)
    assert replay.id == invite.id and "token" not in invite.model_dump_json()
    await harness.auth.invite_initial_admin(platform, second.tenant_id, str(uuid4()))
    onboarding = TenantOnboardingService(harness.sessions)
    assert (await onboarding.get(platform, first.tenant_id)).invitation_status == "queued"

    # A pending administrator has no way in before acceptance.
    with pytest.raises(AuthenticationError):
        await harness.login(first.tenant_id, email)

    token = await harness.token(first.tenant_id, first.admin_id)
    with pytest.raises(AuthenticationError):
        await harness.accept(token[:-1] + ("A" if token[-1] != "A" else "B"))
    await harness.accept(token)
    with pytest.raises(AuthenticationError):
        await harness.accept(token)  # Single use.
    state = await onboarding.get(platform, first.tenant_id)
    assert state.administrator_status == "active" and state.invitation_status == "accepted"
    assert (await onboarding.get(platform, second.tenant_id)).administrator_status == "pending"

    context = await harness.login(first.tenant_id, email)
    assert context.tenant_id == first.tenant_id and context.human_session_id is not None
    me = await UserService(UnitOfWork(harness.sessions)).me(context)
    assert me.id == first.admin_id and me.tenant_id == first.tenant_id
    # The accepted password opens only its own workspace.
    with pytest.raises(AuthenticationError):
        await harness.login(second.tenant_id, email)
    with pytest.raises(AuthenticationError):
        await harness.login(first.tenant_id, email, "wrong password entirely 1")

    # The stored rows are isolated by RLS and never hold the bearer token or password.
    async with harness.sessions.begin() as session:
        await set_tenant_scope(session, second.tenant_id)
        assert (await session.scalars(select(HumanAccount))).all() == []
        assert (await session.scalars(select(HumanSession))).all() == []
    async with harness.sessions.begin() as session:
        await set_tenant_scope(session, first.tenant_id)
        account = (await session.scalars(select(HumanAccount))).one()
        assert not hasattr(account, "password_hash")
    async with tenancy.admin.begin() as session:
        identity = await session.get(HumanIdentity, account.identity_id)
        assert (
            identity is not None
            and identity.password_hash
            and PASSWORD not in identity.password_hash
        )

    await harness.auth.logout(context)
    with pytest.raises(AuthenticationError):
        await UserService(UnitOfWork(harness.sessions)).me(context)


async def test_expired_and_superseded_invitations_are_denied(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    platform = await harness.platform()
    email = f"{uuid4()}@example.test"
    view = await harness.onboard(platform, email)
    await harness.auth.invite_initial_admin(platform, view.tenant_id, str(uuid4()))
    old = await harness.token(view.tenant_id, view.admin_id)
    await harness.auth.invite_initial_admin(platform, view.tenant_id, str(uuid4()))
    fresh = await harness.token(view.tenant_id, view.admin_id)
    assert fresh != old
    with pytest.raises(AuthenticationError):
        await harness.accept(old)  # Resend invalidated the earlier link.
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(AuthChallenge)
            .where(AuthChallenge.user_id == view.admin_id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(AuthenticationError):
        await harness.accept(fresh)
    onboarding = TenantOnboardingService(harness.sessions)
    assert (await onboarding.get(platform, view.tenant_id)).invitation_status == "expired"


async def test_team_management_and_last_admin_protection(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    platform = await harness.platform()
    email = f"{uuid4()}@example.test"
    view = await harness.onboard(platform, email)
    await harness.auth.invite_initial_admin(platform, view.tenant_id, str(uuid4()))
    await harness.accept(await harness.token(view.tenant_id, view.admin_id))
    admin = await harness.login(view.tenant_id, email)

    demote = TeamMemberUpdate(role="member", is_active=True)
    with pytest.raises(ConflictError):
        await harness.auth.manage_member(admin, view.admin_id, demote)
    with pytest.raises(ConflictError):
        await harness.auth.manage_member(
            admin, view.admin_id, TeamMemberUpdate(role="admin", is_active=False)
        )

    member_email = f"{uuid4()}@example.test"
    invited = await harness.auth.invite_member(
        admin, InviteMember(email=member_email, display_name="Second"), str(uuid4())
    )
    with pytest.raises(ConflictError):
        await harness.auth.invite_member(
            admin, InviteMember(email=member_email, display_name="Again"), str(uuid4())
        )
    # A pending member cannot be switched on without accepting the invitation.
    with pytest.raises(ConflictError):
        await harness.auth.manage_member(
            admin, invited.user_id, TeamMemberUpdate(role="admin", is_active=True)
        )
    # Still the only accepted administrator: a pending invitation does not count.
    with pytest.raises(ConflictError):
        await harness.auth.manage_member(admin, view.admin_id, demote)
    await harness.accept(await harness.token(view.tenant_id, invited.user_id))
    member = await harness.login(view.tenant_id, member_email)
    with pytest.raises(AuthorizationError):
        await harness.auth.invite_member(
            member, InviteMember(email=f"{uuid4()}@example.test", display_name="X"), str(uuid4())
        )
    with pytest.raises(AuthorizationError):
        await harness.auth.manage_member(member, view.admin_id, demote)

    await harness.auth.manage_member(
        admin, invited.user_id, TeamMemberUpdate(role="admin", is_active=True)
    )
    # The role change revoked the member's session; the stale role cannot be used.
    with pytest.raises(AuthenticationError):
        await UserService(UnitOfWork(harness.sessions)).me(member)
    promoted = await harness.login(view.tenant_id, member_email)
    # With a second accepted administrator, the first may now step down.
    await harness.auth.manage_member(promoted, view.admin_id, demote)
    with pytest.raises(AuthenticationError):
        await UserService(UnitOfWork(harness.sessions)).me(admin)
    with pytest.raises(ConflictError):
        await harness.auth.manage_member(promoted, invited.user_id, demote)

    # An administrator of another workspace cannot reach this membership.
    with pytest.raises(NotFoundError):
        await harness.auth.manage_member(tenancy.context(tenancy.a), invited.user_id, None)


async def test_support_session_manages_team_and_renames_workspace(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    platform = await harness.platform()
    tenant_id = tenancy.a.tenant_id
    access = SupportAccessService(harness.sessions)
    entered = await access.enter(
        platform, EnterSupportCommand(tenant_id=tenant_id, reason="Setting up the team")
    )
    support = await access.authenticate(entered.access_token, uuid4())
    users = UserService(UnitOfWork(harness.sessions))

    email = f"{uuid4()}@example.test"
    invited = await harness.auth.invite_member(
        support, InviteMember(email=email, display_name="Invited"), str(uuid4())
    )
    listed = {row.id: row for row in await users.list(support, 100, 0)}
    assert listed[invited.user_id].status == "invited"
    await harness.accept(await harness.token(tenant_id, invited.user_id))
    member = await harness.login(tenant_id, email)
    off = TeamMemberUpdate(role="member", is_active=False)
    await harness.auth.manage_member(support, invited.user_id, off)
    with pytest.raises(AuthenticationError):
        await users.me(member)
    listed = {row.id: row for row in await users.list(support, 100, 0)}
    assert listed[invited.user_id].status == "deactivated"

    renamed = await TenantService(UnitOfWork(harness.sessions)).rename(
        support, TenantName(name="Renamed by support")
    )
    assert renamed.name == "Renamed by support"
    # Another workspace stays out of reach of this support session.
    with pytest.raises(NotFoundError):
        await harness.auth.manage_member(support, tenancy.b.user_id, off)
    async with tenancy.admin.begin() as session:
        events = (
            await session.execute(
                select(AuditEvent.action, AuditEvent.actor_type, AuditEvent.support_session_id)
                .where(AuditEvent.tenant_id == tenant_id, AuditEvent.resource_id.is_not(None))
                .where(AuditEvent.action.in_(["team_member_invited", "team_member_updated"]))
            )
        ).all()
    assert {row[0] for row in events} == {"team_member_invited", "team_member_updated"}
    assert all(
        row[1] == "platform_support" and row[2] == support.support_session_id for row in events
    )


async def test_otp_recovery_limits_and_session_revocation(tenancy: TenancyFixture) -> None:
    harness = Harness(tenancy)
    platform = await harness.platform()
    email = f"{uuid4()}@example.test"
    view = await harness.onboard(platform, email)
    await harness.auth.invite_initial_admin(platform, view.tenant_id, str(uuid4()))
    await harness.accept(await harness.token(view.tenant_id, view.admin_id))
    session_before = await harness.login(view.tenant_id, email)
    identity = EmailIdentity(tenant_id=view.tenant_id, email=email)

    # Unknown accounts get the same silent outcome and no challenge.
    await harness.auth.request_reset(
        EmailIdentity(tenant_id=view.tenant_id, email="nobody@example.test"), uuid4()
    )
    await harness.auth.request_reset(identity, uuid4())
    body = (await harness.mail(view.tenant_id, view.admin_id))["body"]
    match = re.search(r"\b(\d{8})\b", body)
    assert match is not None
    code = match.group(1)
    replacement = "an entirely new passphrase 42"

    def attempt(otp: str) -> ResetPassword:
        return ResetPassword(
            tenant_id=view.tenant_id,
            email=email,
            otp=SecretStr(otp),
            password=SecretStr(replacement),
        )

    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            await harness.auth.reset(attempt(wrong), uuid4())
    # Five failures persisted: even the right code is now refused.
    with pytest.raises(AuthenticationError):
        await harness.auth.reset(attempt(code), uuid4())
    assert (await harness.login(view.tenant_id, email)).tenant_id == view.tenant_id

    await harness.auth.request_reset(identity, uuid4())
    body = (await harness.mail(view.tenant_id, view.admin_id))["body"]
    fresh = re.search(r"\b(\d{8})\b", body)
    assert fresh is not None
    await harness.auth.reset(attempt(fresh.group(1)), uuid4())
    with pytest.raises(AuthenticationError):
        await harness.auth.reset(attempt(fresh.group(1)), uuid4())  # Consumed.
    with pytest.raises(AuthenticationError):
        await UserService(UnitOfWork(harness.sessions)).me(session_before)
    with pytest.raises(AuthenticationError):
        await harness.login(view.tenant_id, email)
    assert (await harness.login(view.tenant_id, email, replacement)).actor_id == view.admin_id


async def test_mail_worker_delivers_once_and_erases_payload(
    tenancy: TenancyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tenancy)
    assert tenancy.settings.worker_database_url is not None
    platform = await harness.platform()
    view = await harness.onboard(platform, f"{uuid4()}@example.test")
    invite = await harness.auth.invite_initial_admin(platform, view.tenant_id, str(uuid4()))
    sent: list[dict[str, str]] = []

    def record(_settings: Settings, payload: dict[str, str], _identity: str) -> None:
        sent.append(payload)

    monkeypatch.setattr("app.auth.mail_worker.send_smtp", record)
    worker = Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    try:
        # Other tests may have queued mail; drain until this invitation is delivered.
        for _ in range(200):
            if not await deliver_one(worker.session_factory, harness.settings):
                break
    finally:
        await worker.close()
    mine = [item for item in sent if "#token=fhi1." + str(view.tenant_id) in item["body"]]
    assert len(mine) == 1 and mine[0]["to"] == view.draft.administrator.email
    async with tenancy.admin.begin() as session:
        row = (
            await session.scalars(select(AuthMail).where(AuthMail.challenge_id == invite.id))
        ).one()
        assert row.status == "sent" and row.ciphertext is None
        user = await session.get(User, view.admin_id)
        assert user is not None and not user.is_active
    onboarding = TenantOnboardingService(harness.sessions)
    assert (await onboarding.get(platform, view.tenant_id)).invitation_status == "sent"
