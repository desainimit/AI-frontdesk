"""One sign-in for everyone: invitations, workspaces, two-step verification, passwords."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, update

from app.audit.models import AuditEvent
from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from app.core.redis import RedisClient
from app.identity.mail import deliver_one
from app.identity.models import Account, AccountMfa, AccountSession, AuthChallenge, AuthMail
from app.identity.schemas import (
    ChangePassword,
    DisableMfa,
    ForgotPassword,
    InviteMember,
    LinkToken,
    MfaCode,
    MfaProof,
    ResetPassword,
    SelectWorkspace,
    TeamMemberUpdate,
)
from app.identity.service import IdentityService
from app.identity.sessions import WorkspaceRequiredError, authenticate_platform
from app.identity.throttle import (
    AuthenticationUnavailableError,
    LoginThrottledError,
    check_login_limit,
)
from app.identity.totp import totp
from app.main import create_app
from app.platform.models import PlatformAuditEvent
from app.users.models import User
from app.users.service import UserService
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.integration.workspaces import PASSWORD, member_role

NEW_PASSWORD = "an entirely different passphrase"


async def current_code(harness: IdentityHarness, account_id: UUID) -> str:
    """The authenticator's current code. Clears the used-code marker so one test can
    present several codes without waiting 30 seconds between them."""
    async with harness.tenancy.admin.begin() as session:
        factor = (
            await session.scalars(select(AccountMfa).where(AccountMfa.account_id == account_id))
        ).one()
        factor.last_counter = -1
        secret = harness.cipher.decrypt(
            factor.key_id, factor.ciphertext, f"account-mfa:{account_id}"
        ).decode()
    return totp(secret, int(datetime.now(UTC).timestamp()) // 30)


async def activity(harness: IdentityHarness, account_id: UUID) -> list[str]:
    async with harness.tenancy.admin.begin() as session:
        return list(
            await session.scalars(
                select(PlatformAuditEvent.action)
                .where(PlatformAuditEvent.user_id == account_id)
                .order_by(PlatformAuditEvent.created_at, PlatformAuditEvent.id)
            )
        )


async def test_invitation_first_sign_in_and_single_workspace(tenancy: TenancyFixture) -> None:
    harness = IdentityHarness(tenancy)
    platform = await harness.platform()
    email = f"{uuid4()}@example.test"
    clinic = await harness.onboard(platform, email)
    assert clinic.invitation_status == "not_sent" and clinic.service_state == "setup"

    key = str(uuid4())
    invite = await harness.invitations.invite_initial_admin(platform, clinic.tenant_id, key)
    replay = await harness.invitations.invite_initial_admin(platform, clinic.tenant_id, key)
    assert replay.id == invite.id and "token" not in invite.model_dump_json()
    assert await harness.invitation_status(platform, clinic.tenant_id) == "queued"

    # The invited person has an account but no password: there is no way in yet.
    with pytest.raises(AuthenticationError):
        await harness.login(email)

    message = await harness.mail(user_id=clinic.admin_id)
    assert message["to"] == email and "https://app.example.test/invite#token=" in message["body"]
    # Nobody is asked to read or type an identifier.
    assert str(clinic.tenant_id) not in message["body"] + message["subject"]
    token = await harness.link_token(user_id=clinic.admin_id)
    details = await harness.invitations.details(LinkToken(token=SecretStr(token)))
    assert details.email == email and details.password_required
    assert details.role_name == "Owner" and details.workspace_name.startswith("Clinic ")
    with pytest.raises(AuthenticationError):
        await harness.accept(token[:-1] + ("A" if token[-1] != "A" else "B"))
    with pytest.raises(ConflictError):
        await harness.accept(token, password=None)  # A new account must choose a password.

    accepted = await harness.accept(token)
    assert accepted is not None and accepted.next == "workspace"
    context = await harness.context(accepted.access_token)
    assert context.tenant_id == clinic.tenant_id and context.actor_id == clinic.admin_id
    with pytest.raises(AuthenticationError):
        await harness.accept(token)  # Single use.
    assert await harness.invitation_status(platform, clinic.tenant_id) == "accepted"

    # From now on: email and password, straight into the one workspace.
    signed_in = await harness.login(email.upper())
    assert signed_in.next == "workspace" and signed_in.access_token != accepted.access_token
    me = await harness.identity.me(signed_in.access_token, uuid4())
    assert me.account.email == email and not me.account.is_platform_admin
    assert not me.account.two_step_enabled and me.next == "workspace"
    assert me.workspace is not None and me.workspace.id == clinic.tenant_id
    assert me.workspace.role_name == "Owner" and me.workspace.support is None
    assert "user:manage" in me.workspace.permissions
    with pytest.raises(AuthenticationError):
        await harness.login(email, "not the password")
    with pytest.raises(AuthenticationError):
        await harness.login(f"{uuid4()}@example.test")
    # A workspace member is not a platform administrator.
    with pytest.raises(AuthorizationError):
        await authenticate_platform(harness.sessions, signed_in.access_token, uuid4())
    assert (await activity(harness, me.account.id)) == ["signed_in", "signed_in"]


async def test_several_workspaces_are_chosen_from_a_list(tenancy: TenancyFixture) -> None:
    harness = IdentityHarness(tenancy)
    # The fixture's owner belongs to both workspaces with one account.
    signed_in = await harness.login(tenancy.a.email)
    assert signed_in.next == "choose_workspace"
    token = signed_in.access_token
    with pytest.raises(WorkspaceRequiredError):
        await harness.context(token)
    options = await harness.identity.workspaces(token)
    assert [(item.name, item.id) for item in options] == [
        ("Tenant A", tenancy.a.tenant_id),
        ("Tenant B", tenancy.b.tenant_id),
    ]
    me = await harness.identity.me(token, uuid4())
    assert me.workspace is None and me.next == "choose_workspace"

    chosen = await harness.identity.select_workspace(
        token, SelectWorkspace(workspace_id=tenancy.b.tenant_id), uuid4()
    )
    assert chosen.workspace is not None and chosen.workspace.name == "Tenant B"
    context = await harness.context(token)
    assert (context.tenant_id, context.actor_id) == (tenancy.b.tenant_id, tenancy.b.user_id)
    users = await UserService(UnitOfWork(harness.sessions)).list(context, 10, 0)
    assert [user.id for user in users] == [tenancy.b.user_id]

    # Switching is one call; a workspace the account does not belong to is refused.
    await harness.identity.select_workspace(
        token, SelectWorkspace(workspace_id=tenancy.a.tenant_id), uuid4()
    )
    assert (await harness.context(token)).tenant_id == tenancy.a.tenant_id
    stranger = await harness.onboard(await harness.platform(), f"{uuid4()}@example.test")
    with pytest.raises(AuthorizationError):
        await harness.identity.select_workspace(
            token, SelectWorkspace(workspace_id=stranger.tenant_id), uuid4()
        )
    assert (await harness.context(token)).tenant_id == tenancy.a.tenant_id

    # A deactivated membership drops out of the list and out of the session.
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(User).where(User.id == tenancy.a.user_id).values(is_active=False)
        )
    with pytest.raises(WorkspaceRequiredError):
        await harness.context(token)
    assert [item.id for item in await harness.identity.workspaces(token)] == [tenancy.b.tenant_id]
    left = await harness.identity.leave_workspace(token, uuid4())
    assert left.workspace is None
    # With one workspace left, the next sign-in goes straight in.
    assert (await harness.login(tenancy.a.email)).next == "workspace"
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenancy.b.tenant_id)
        entered = (
            await session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.tenant_id == tenancy.b.tenant_id,
                    AuditEvent.action == "workspace_entered",
                )
            )
        ).all()
        assert len(entered) == 2


async def test_existing_account_joins_another_workspace_with_its_own_password(
    tenancy: TenancyFixture,
) -> None:
    harness = IdentityHarness(tenancy)
    owner = tenancy.context(tenancy.a)
    member_email = f"{uuid4()}@example.test"
    # First membership: a new account.
    first = await harness.invitations.invite_member(
        owner,
        InviteMember(
            email=member_email.upper(),
            display_name="Member",
            role_id=member_role(tenancy.a.tenant_id),
        ),
        str(uuid4()),
    )
    session = await harness.accept(await harness.link_token(user_id=first.user_id))
    assert session is not None
    # Second workspace invites the same address.
    second = await harness.invitations.invite_member(
        tenancy.context(tenancy.b),
        InviteMember(
            email=member_email,
            display_name="Member again",
            role_id=member_role(tenancy.b.tenant_id),
        ),
        str(uuid4()),
    )
    token = await harness.link_token(user_id=second.user_id)
    details = await harness.invitations.details(LinkToken(token=SecretStr(token)))
    assert not details.password_required and details.workspace_name == "Tenant B"
    with pytest.raises(ConflictError):
        await harness.accept(token, NEW_PASSWORD)  # An invitation never replaces a password.
    assert await harness.accept(token, password=None) is None
    assert (await harness.login(member_email)).next == "choose_workspace"
    with pytest.raises(AuthenticationError):
        await harness.login(member_email, NEW_PASSWORD)
    async with tenancy.admin.begin() as db:
        accounts = (await db.scalars(select(Account).where(Account.email == member_email))).all()
        assert len(accounts) == 1 and not accounts[0].is_platform_admin


async def test_two_step_verification_is_optional_and_then_required(
    tenancy: TenancyFixture,
) -> None:
    harness = IdentityHarness(tenancy)
    account_id = tenancy.b.account_id
    first = await harness.login(tenancy.b.email)
    other = await harness.login(tenancy.b.email)
    token = first.access_token

    setup = await harness.mfa.setup(token, uuid4())
    assert setup.qr_image.startswith("data:image/svg+xml;") and len(setup.secret) == 32
    assert "secret=" + setup.secret in setup.provisioning_uri
    # Nothing changes until a code proves the app was set up.
    assert (await harness.login(tenancy.b.email)).next == "choose_workspace"
    with pytest.raises(AuthenticationError):
        await harness.mfa.enable(token, MfaCode(code=SecretStr("000000")), uuid4())
    restarted = await harness.mfa.setup(token, uuid4())
    assert restarted.secret != setup.secret
    enabled = await harness.mfa.enable(
        token, MfaCode(code=SecretStr(await current_code(harness, account_id))), uuid4()
    )
    assert len(enabled.codes) == 8 and all(code.startswith("FD-") for code in enabled.codes)
    with pytest.raises(ConflictError):
        await harness.mfa.setup(token, uuid4())  # Already on.
    me = await harness.identity.me(token, uuid4())
    assert me.account.two_step_enabled and me.account.recovery_codes_remaining == 8
    # Browsers that signed in with the password alone are signed out.
    with pytest.raises(AuthenticationError):
        await harness.identity.me(other.access_token, uuid4())

    # Sign-in now stops for the code; until then the session opens nothing.
    pending = await harness.login(tenancy.b.email)
    assert pending.next == "mfa"
    for call in (
        harness.identity.me(pending.access_token, uuid4()),
        harness.identity.workspaces(pending.access_token),
        harness.context(pending.access_token),
        harness.mfa.setup(pending.access_token, uuid4()),
    ):
        with pytest.raises(AuthenticationError):
            await call
    with pytest.raises(AuthenticationError):
        await harness.identity.login_mfa(
            pending.access_token, MfaProof(code=SecretStr("000000")), uuid4()
        )
    code = await current_code(harness, account_id)
    done = await harness.identity.login_mfa(
        pending.access_token, MfaProof(code=SecretStr(code)), uuid4()
    )
    assert done.next == "choose_workspace" and done.expires_at > pending.expires_at
    assert (await harness.identity.me(pending.access_token, uuid4())).account.two_step_enabled
    # The same code cannot finish a second sign-in, and a finished session is not "pending".
    again = await harness.login(tenancy.b.email)
    with pytest.raises(AuthenticationError):
        await harness.identity.login_mfa(
            again.access_token, MfaProof(code=SecretStr(code)), uuid4()
        )
    with pytest.raises(AuthenticationError):
        await harness.identity.login_mfa(
            pending.access_token, MfaProof(code=SecretStr("000000")), uuid4()
        )

    # A recovery code works once and signs every other browser out.
    recovered = await harness.identity.login_mfa(
        again.access_token, MfaProof(recovery_code=SecretStr(enabled.codes[0])), uuid4()
    )
    assert recovered.next == "choose_workspace"
    for stale in (token, pending.access_token):
        with pytest.raises(AuthenticationError):
            await harness.identity.me(stale, uuid4())
    token = again.access_token
    assert (await harness.identity.me(token, uuid4())).account.recovery_codes_remaining == 7
    retry = await harness.login(tenancy.b.email)
    with pytest.raises(AuthenticationError):
        await harness.identity.login_mfa(
            retry.access_token, MfaProof(recovery_code=SecretStr(enabled.codes[0])), uuid4()
        )

    # New recovery codes replace the old ones.
    replaced = await harness.mfa.recovery_codes(
        token, MfaCode(code=SecretStr(await current_code(harness, account_id))), uuid4()
    )
    assert set(replaced.codes).isdisjoint(enabled.codes)
    with pytest.raises(AuthenticationError):
        await harness.identity.login_mfa(
            retry.access_token, MfaProof(recovery_code=SecretStr(enabled.codes[1])), uuid4()
        )

    # Turning it off needs the password and a code.
    with pytest.raises(AuthenticationError):
        await harness.mfa.disable(
            token,
            DisableMfa(
                password=SecretStr("wrong"),
                code=SecretStr(await current_code(harness, account_id)),
            ),
            uuid4(),
        )
    await harness.mfa.disable(
        token,
        DisableMfa(
            password=SecretStr(PASSWORD), code=SecretStr(await current_code(harness, account_id))
        ),
        uuid4(),
    )
    assert not (await harness.identity.me(token, uuid4())).account.two_step_enabled
    assert (await harness.login(tenancy.b.email)).next == "choose_workspace"
    actions = await activity(harness, account_id)
    for expected in (
        "two_step_setup_started",
        "two_step_enabled",
        "two_step_failed",
        "recovery_code_used",
        "recovery_codes_replaced",
        "two_step_disabled",
    ):
        assert expected in actions


async def test_repeated_wrong_codes_lock_the_factor_but_not_the_password(
    tenancy: TenancyFixture,
) -> None:
    harness = IdentityHarness(tenancy)
    account_id = tenancy.a.account_id
    token = (await harness.login(tenancy.a.email)).access_token
    await harness.mfa.setup(token, uuid4())
    await harness.mfa.enable(
        token, MfaCode(code=SecretStr(await current_code(harness, account_id))), uuid4()
    )
    pending = await harness.login(tenancy.a.email)
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            await harness.identity.login_mfa(
                pending.access_token, MfaProof(code=SecretStr("000000")), uuid4()
            )
    # Locked: even the right code is refused for a while.
    with pytest.raises(AuthenticationError):
        await harness.identity.login_mfa(
            pending.access_token,
            MfaProof(code=SecretStr(await current_code(harness, account_id))),
            uuid4(),
        )
    async with tenancy.admin.begin() as session:
        factor = (
            await session.scalars(select(AccountMfa).where(AccountMfa.account_id == account_id))
        ).one()
        assert factor.locked_until is not None and factor.locked_until > datetime.now(UTC)
        factor.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    assert (
        await harness.identity.login_mfa(
            pending.access_token,
            MfaProof(code=SecretStr(await current_code(harness, account_id))),
            uuid4(),
        )
    ).next == "choose_workspace"


async def test_password_change_and_emailed_reset(tenancy: TenancyFixture) -> None:
    harness = IdentityHarness(tenancy)
    email, account_id = tenancy.a.email, tenancy.a.account_id
    here = (await harness.login(email)).access_token
    elsewhere = (await harness.login(email)).access_token

    with pytest.raises(AuthenticationError):
        await harness.identity.change_password(
            here,
            ChangePassword(current_password=SecretStr("wrong"), password=SecretStr(NEW_PASSWORD)),
            uuid4(),
        )
    await harness.identity.change_password(
        here,
        ChangePassword(current_password=SecretStr(PASSWORD), password=SecretStr(NEW_PASSWORD)),
        uuid4(),
    )
    # This browser stays signed in; every other session, and the old password, stop working.
    assert (await harness.identity.me(here, uuid4())).account.email == email
    for stale in (elsewhere, tenancy.a.token.get_secret_value()):
        with pytest.raises(AuthenticationError):
            await harness.identity.me(stale, uuid4())
    with pytest.raises(AuthenticationError):
        await harness.login(email)
    assert (await harness.login(email, NEW_PASSWORD)).next == "choose_workspace"

    # Forgotten password: a link by email. Unknown addresses get the same answer and no mail.
    await harness.identity.forgot_password(ForgotPassword(email=f"{uuid4()}@example.test"), uuid4())
    await harness.identity.forgot_password(ForgotPassword(email=email.upper()), uuid4())
    message = await harness.mail(email=email)
    assert "https://app.example.test/reset-password#token=" in message["body"]
    first_link = await harness.link_token(email=email)
    await harness.identity.forgot_password(ForgotPassword(email=email), uuid4())
    second_link = await harness.link_token(email=email)
    assert first_link != second_link

    final = "the third and final passphrase"

    def reset(token: str) -> ResetPassword:
        return ResetPassword(token=SecretStr(token), password=SecretStr(final))

    # Asking again cancels the earlier link.
    with pytest.raises(AuthenticationError):
        await harness.identity.reset_password(reset(first_link), uuid4())
    with pytest.raises(AuthenticationError):
        await harness.identity.reset_password(reset(second_link + "x"), uuid4())
    await harness.identity.reset_password(reset(second_link), uuid4())
    with pytest.raises(AuthenticationError):
        await harness.identity.reset_password(reset(second_link), uuid4())  # Single use.
    with pytest.raises(AuthenticationError):
        await harness.identity.me(here, uuid4())  # Every session ends.
    with pytest.raises(AuthenticationError):
        await harness.login(email, NEW_PASSWORD)
    assert (await harness.login(email, final)).next == "choose_workspace"

    # A link stops working if the password changes after it was sent, and when it expires.
    await harness.identity.forgot_password(ForgotPassword(email=email), uuid4())
    stale_link = await harness.link_token(email=email)
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(credential_revision=Account.credential_revision + 1)
        )
    with pytest.raises(AuthenticationError):
        await harness.identity.reset_password(reset(stale_link), uuid4())
    await harness.identity.forgot_password(ForgotPassword(email=email), uuid4())
    late_link = await harness.link_token(email=email)
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(AuthChallenge)
            .where(AuthChallenge.account_id == account_id, AuthChallenge.consumed_at.is_(None))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    with pytest.raises(AuthenticationError):
        await harness.identity.reset_password(reset(late_link), uuid4())
    actions = await activity(harness, account_id)
    assert actions.count("password_reset_requested") == 4
    assert "password_changed" in actions and "password_reset_completed" in actions
    # Without mail configured the request is refused, not silently dropped.
    with pytest.raises(AuthenticationUnavailableError):
        await IdentityService(harness.sessions, tenancy.settings).forgot_password(
            ForgotPassword(email=email), uuid4()
        )


async def test_team_invitations_roles_and_last_administrator(tenancy: TenancyFixture) -> None:
    harness = IdentityHarness(tenancy)
    owner = tenancy.context(tenancy.a)
    email = f"{uuid4()}@example.test"
    key = str(uuid4())
    invite = await harness.invitations.invite_member(
        owner,
        InviteMember(
            email=email,
            display_name="Front Desk",
            role_id=member_role(tenancy.a.tenant_id),
        ),
        key,
    )
    assert (await harness.invitations.invite_member(owner, invite.user_id, key)).id == invite.id
    with pytest.raises(ConflictError):  # Already a member of this workspace.
        await harness.invitations.invite_member(
            owner,
            InviteMember(
                email=email.upper(),
                display_name="Again",
                role_id=member_role(tenancy.a.tenant_id),
            ),
            str(uuid4()),
        )
    listed = await UserService(UnitOfWork(harness.sessions)).list(owner, 10, 0)
    assert {user.status for user in listed} == {"active", "invited"}
    # A pending member cannot be switched on by editing them.
    with pytest.raises(ConflictError):
        await harness.invitations.manage_member(
            owner, invite.user_id, TeamMemberUpdate(is_active=True)
        )

    # Resending replaces the link; only the newest one works.
    first_link = await harness.link_token(user_id=invite.user_id)
    await harness.invitations.invite_member(owner, invite.user_id, str(uuid4()))
    second_link = await harness.link_token(user_id=invite.user_id)
    with pytest.raises(AuthenticationError):
        await harness.accept(first_link)
    session = await harness.accept(second_link)
    assert session is not None
    member = await harness.context(session.access_token)
    assert member.role_name == "Member"
    with pytest.raises(AuthorizationError):
        await harness.invitations.invite_member(
            member,
            InviteMember(
                email=f"{uuid4()}@example.test",
                display_name="No",
                role_id=member_role(tenancy.a.tenant_id),
            ),
            str(uuid4()),
        )
    with pytest.raises(ConflictError):  # Accepted: there is nothing left to resend.
        await harness.invitations.invite_member(owner, invite.user_id, str(uuid4()))

    # Deactivating signs the member out of this workspace at once.
    await harness.invitations.manage_member(
        owner, invite.user_id, TeamMemberUpdate(is_active=False)
    )
    with pytest.raises(AuthenticationError):
        await harness.identity.me(session.access_token, uuid4())
    with pytest.raises(AuthenticationError):
        async with UnitOfWork(harness.sessions).transaction(member):
            pass
    signed_in = await harness.login(email)
    assert signed_in.next == "choose_workspace"
    assert await harness.identity.workspaces(signed_in.access_token) == []
    await harness.invitations.manage_member(owner, invite.user_id, TeamMemberUpdate(is_active=True))
    assert (await harness.login(email)).next == "workspace"
    # "Sign out everywhere" for one member, without changing them.
    live = (await harness.login(email)).access_token
    await harness.invitations.manage_member(owner, invite.user_id, None)
    with pytest.raises(AuthenticationError):
        await harness.identity.me(live, uuid4())

    # The last administrator cannot be demoted or deactivated.
    for change in (
        TeamMemberUpdate(role_id=member_role(tenancy.a.tenant_id), is_active=True),
        TeamMemberUpdate(is_active=False),
    ):
        with pytest.raises(ConflictError):
            await harness.invitations.manage_member(owner, tenancy.a.user_id, change)
    with pytest.raises(NotFoundError):  # Another workspace's member is invisible.
        await harness.invitations.manage_member(
            owner, tenancy.b.user_id, TeamMemberUpdate(is_active=True)
        )
    async with tenancy.admin.begin() as db:
        await set_tenant_scope(db, tenancy.a.tenant_id)
        actions = set(
            await db.scalars(select(AuditEvent.action).where(AuditEvent.resource_type == "user"))
        )
    assert {"team_member_invited", "invitation_accepted", "team_member_updated"} <= actions


async def test_mail_worker_delivers_once_and_erases_the_message(
    tenancy: TenancyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = IdentityHarness(tenancy)
    assert tenancy.settings.worker_database_url is not None
    worker = Database(
        harness.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    await harness.identity.forgot_password(ForgotPassword(email=tenancy.a.email), uuid4())
    sent: list[dict[str, str]] = []

    def record(settings: object, payload: dict[str, str], identity: str) -> None:
        sent.append(payload)

    monkeypatch.setattr("app.identity.mail.send_smtp", record)
    try:
        # Drain whatever other tests left queued, then nothing more is delivered.
        while await deliver_one(worker.session_factory, harness.settings):
            pass
        assert not await deliver_one(worker.session_factory, harness.settings)
    finally:
        await worker.close()
    mine = [item for item in sent if item["to"] == tenancy.a.email]
    assert len(mine) == 1 and "/reset-password#token=" in mine[0]["body"]
    async with tenancy.admin.begin() as session:
        row = (
            await session.execute(
                select(AuthMail)
                .join(AuthChallenge, AuthChallenge.id == AuthMail.challenge_id)
                .where(AuthChallenge.account_id == tenancy.a.account_id)
            )
        ).scalar_one()
        # Sent, and the text (with its link) is gone from the database.
        assert row.status == "sent" and row.ciphertext is None


async def test_sign_in_over_http_never_takes_a_workspace_from_the_browser(
    tenancy: TenancyFixture,
) -> None:
    harness = IdentityHarness(tenancy)
    app = create_app(harness.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        wrong = await client.post(
            "/api/v1/auth/login", json={"email": tenancy.a.email, "password": "nope"}
        )
        assert wrong.status_code == 401 and wrong.headers["cache-control"] == "no-store"
        assert wrong.json()["error"]["code"] == "unauthenticated"
        # A workspace identifier in the request is not part of sign-in.
        extra = await client.post(
            "/api/v1/auth/login",
            json={
                "email": tenancy.a.email,
                "password": PASSWORD,
                "tenant_id": str(tenancy.a.tenant_id),
            },
        )
        assert extra.status_code == 422
        login = await client.post(
            "/api/v1/auth/login", json={"email": tenancy.a.email, "password": PASSWORD}
        )
        assert login.status_code == 200 and login.headers["cache-control"] == "no-store"
        body = login.json()
        assert body["next"] == "choose_workspace" and body["access_token"].startswith("fs1.")
        headers = {"Authorization": f"Bearer {body['access_token']}"}

        # No workspace yet: workspace routes say so, with a code the browser can act on.
        blocked = await client.get("/api/v1/tenants/current", headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "workspace_required"
        options = (await client.get("/api/v1/auth/workspaces", headers=headers)).json()
        assert [item["name"] for item in options] == ["Tenant A", "Tenant B"]
        chosen = await client.post(
            "/api/v1/auth/workspace", headers=headers, json={"workspace_id": options[0]["id"]}
        )
        assert chosen.status_code == 200 and chosen.json()["workspace"]["name"] == "Tenant A"
        current = await client.get("/api/v1/tenants/current", headers=headers)
        assert current.status_code == 200 and current.json()["id"] == str(tenancy.a.tenant_id)
        me = (await client.get("/api/v1/auth/me", headers=headers)).json()
        assert me["account"]["email"] == tenancy.a.email and me["next"] == "workspace"
        assert "password" not in str(me) and "token" not in str(me)
        # The platform console is closed to workspace members.
        assert (await client.get("/api/v1/platform/tenants", headers=headers)).status_code == 403

        setup = await client.post("/api/v1/auth/mfa/setup", headers=headers)
        assert setup.status_code == 200 and setup.json()["qr_image"].startswith("data:image/svg")
        changed = await client.post(
            "/api/v1/auth/password/change",
            headers=headers,
            json={"current_password": PASSWORD, "password": "short"},
        )
        assert changed.status_code == 422 and "short" not in changed.text
        forgot = await client.post("/api/v1/auth/password/forgot", json={"email": "x@y.test"})
        assert forgot.status_code == 202 and forgot.json() == {"status": "accepted"}

        left = await client.post("/api/v1/auth/workspace/leave", headers=headers)
        assert left.status_code == 200 and left.json()["workspace"] is None
        assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 204
        assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    async with tenancy.admin.begin() as session:
        row = (
            await session.scalars(
                select(AccountSession).where(
                    AccountSession.id == UUID(body["access_token"].split(".")[1])
                )
            )
        ).one()
        assert row.revoked_at is not None and row.token_digest not in body["access_token"]


async def test_sign_in_guesses_are_counted_in_redis(tenancy: TenancyFixture) -> None:
    redis = RedisClient(tenancy.settings)
    email, address = f"{uuid4()}@example.test", f"198.51.100.{uuid4().int % 250}"
    try:
        for _ in range(5):
            await check_login_limit(redis, email, address, scope="login")
        with pytest.raises(LoginThrottledError):
            await check_login_limit(redis, email, address, scope="login")
        # Another account from the same address, and another kind of attempt, still work.
        await check_login_limit(redis, f"{uuid4()}@example.test", address, scope="login")
        await check_login_limit(redis, email, f"{address}1", scope="password-forgot")
    finally:
        await redis.close()
    app = create_app(tenancy.settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        statuses = [
            (
                await client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
            ).status_code
            for _ in range(2)
        ]
        assert statuses == [429, 429]
