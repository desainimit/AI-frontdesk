"""Inviting people to a workspace, accepting an invitation, and managing members.

An invited person is a membership that is not active yet, linked to the account for
their email address. Accepting the emailed link proves they read that mailbox; a new
account chooses its password at that moment, an existing account keeps its own.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import AuthenticationError, AuthorizationError, ConflictError, NotFoundError
from app.core.settings import Settings
from app.events.idempotency import InvalidIdempotencyKeyError
from app.identity.mail import queue_mail
from app.identity.models import Account, AuthChallenge, AuthMail
from app.identity.passwords import hash_password
from app.identity.schemas import (
    AcceptInvitation,
    InvitationDetails,
    InvitationView,
    InviteMember,
    LinkToken,
    SessionView,
    TeamMemberUpdate,
)
from app.identity.service import cancel_challenges, record, revoke_sessions
from app.identity.sessions import WORKSPACE_SESSION, issue_session, platform_scope
from app.identity.throttle import AuthenticationUnavailableError
from app.identity.tokens import INVITATION, digest_token, issue_token, parse_token, token_matches
from app.platform.onboarding_models import TenantOnboarding
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.rbac.models import TenantRole
from app.rbac.service import assert_custodian, grantable_role
from app.tenants.models import Tenant
from app.users.models import User

INVITATION_LINK = timedelta(hours=24)
_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}")


async def ensure_account(db: AsyncSession, email: str, display_name: str) -> Account:
    """The account for an email address, created without a password if there is none."""
    normalized = email.strip().lower()
    await db.execute(
        insert(Account)
        .values(id=uuid4(), email=normalized, display_name=display_name)
        .on_conflict_do_nothing(index_elements=[Account.email])
    )
    account = await db.scalar(select(Account).where(Account.email == normalized))
    if account is None:
        raise ConflictError()
    return account


async def membership_state(db: AsyncSession, user: User) -> tuple[str, str]:
    """(member status, invitation status) from what is stored, never from a token."""
    if user.accepted_at is not None:
        return ("active" if user.is_active else "deactivated"), "accepted"
    row = (
        await db.execute(
            select(AuthChallenge.expires_at, AuthMail.status)
            .join(AuthMail, AuthMail.challenge_id == AuthChallenge.id, isouter=True)
            .where(
                AuthChallenge.user_id == user.id,
                AuthChallenge.purpose == "invitation",
                AuthChallenge.consumed_at.is_(None),
            )
            .order_by(AuthChallenge.created_at.desc(), AuthChallenge.id.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return "pending", "not_sent"
    if row[0] <= datetime.now(UTC):
        return "pending", "expired"
    return "pending", {"sent": "sent", "failed": "delivery_failed"}.get(row[1] or "", "queued")


async def _tenant_lock(db: AsyncSession, tenant_id: UUID) -> Tenant:
    await set_tenant_scope(db, tenant_id)
    tenant = await db.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None or not tenant.is_active:
        raise AuthenticationError()
    return tenant


async def _team_actor(db: AsyncSession, context: RequestContext) -> None:
    """Serialize team changes per workspace, then recheck who is making this one."""
    if context.actor_type not in {ActorType.USER, ActorType.PLATFORM_SUPPORT}:
        raise AuthorizationError()
    await _tenant_lock(db, context.tenant_id)
    await enter_tenant_scope(db, context)


def _team_audit(db: AsyncSession, context: RequestContext, action: str, user_id: UUID) -> None:
    db.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            platform_user_id=context.platform_user_id,
            support_session_id=context.support_session_id,
            actor_type=context.actor_type,
            action=action,
            resource_type="user",
            resource_id=user_id,
            request_id=context.request_id,
        )
    )


def _checked_key(key: str) -> None:
    if not _KEY.fullmatch(key):
        raise InvalidIdempotencyKeyError()


async def _view(db: AsyncSession, challenge: AuthChallenge) -> InvitationView:
    mail = await db.scalar(select(AuthMail).where(AuthMail.challenge_id == challenge.id))
    if challenge.user_id is None:
        raise NotFoundError()
    return InvitationView(
        id=challenge.id,
        user_id=challenge.user_id,
        expires_at=challenge.expires_at,
        delivery_status=mail.status if mail else "unavailable",
        consumed=challenge.consumed_at is not None,
    )


class InvitationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions, self._settings = sessions, settings

    def _mail_ready(self) -> None:
        if not self._settings.account_mail_ready:
            raise AuthenticationUnavailableError()

    async def queue_invitation(
        self, db: AsyncSession, user: User, key_digest: str
    ) -> AuthChallenge:
        """Replace any earlier invitation for this membership and email a fresh link."""
        self._mail_ready()
        if user.accepted_at is not None:
            raise ConflictError()
        await cancel_challenges(db, purpose="invitation", user_id=user.id)
        identity, token, digest = issue_token(INVITATION)
        challenge = AuthChallenge(
            id=identity,
            account_id=user.account_id,
            tenant_id=user.tenant_id,
            user_id=user.id,
            purpose="invitation",
            key_digest=key_digest,
            token_digest=digest,
            expires_at=datetime.now(UTC) + INVITATION_LINK,
        )
        db.add(challenge)
        tenant = await db.get(Tenant, user.tenant_id)
        role = await db.get(TenantRole, user.role_id)
        account = await db.get(Account, user.account_id)
        origin = self._settings.tenant_auth_public_origin.rstrip("/")
        step = (
            "Open this link to accept and choose your password"
            if account is None or account.password_hash is None
            else "Open this link to accept. You will sign in with your existing password"
        )
        await queue_mail(
            db,
            self._settings,
            challenge,
            user.email,
            f"You are invited to {tenant.name if tenant else 'a workspace'} on AI Frontdesk",
            f"Hello {user.display_name},\n\n"
            f"You have been invited to {tenant.name if tenant else 'a workspace'}"
            f"{f' as {role.name}' if role else ''}.\n\n"
            f"{step}:\n"
            # Fragment, not query: the token is never sent to servers or kept in logs.
            f"{origin}/invite#token={token}\n\n"
            "The link works once and expires in 24 hours. If you did not expect this, "
            "you can ignore this email.",
        )
        return challenge

    async def invite_initial_admin(
        self, context: PlatformContext, tenant_id: UUID, key: str
    ) -> InvitationView:
        """Send (or resend) the first administrator's invitation for a new workspace."""
        self._mail_ready()
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        _checked_key(key)
        digest = digest_token(f"platform-invite:{context.user_id}:{tenant_id}:{key}")
        async with self._sessions.begin() as db:
            await platform_scope(db, context)
            await _tenant_lock(db, tenant_id)
            row = await db.scalar(
                select(TenantOnboarding).where(TenantOnboarding.tenant_id == tenant_id)
            )
            if row is None:
                raise NotFoundError()
            existing = await db.scalar(
                select(AuthChallenge).where(AuthChallenge.key_digest == digest)
            )
            if existing is None:
                user = await db.get(User, row.admin_id)
                if user is None:
                    raise NotFoundError()
                existing = await self.queue_invitation(db, user, digest)
                row.status = "invited"
                row.revision += 1
                await db.flush()
                await record(
                    db,
                    context.user_id,
                    "tenant_admin_invited",
                    context.request_id,
                    tenant_id=tenant_id,
                )
            return await _view(db, existing)

    async def invite_member(
        self, context: RequestContext, command: InviteMember | UUID, key: str
    ) -> InvitationView:
        """Invite a new member, or resend to a pending one. A repeat returns the same invite."""
        self._mail_ready()
        require_permission(context, Permission.USER_INVITE)
        _checked_key(key)
        principal = context.actor_id or context.platform_user_id
        digest = digest_token(f"member-invite:{principal}:{context.tenant_id}:{key}")
        async with self._sessions.begin() as db:
            await _team_actor(db, context)
            existing = await db.scalar(
                select(AuthChallenge).where(AuthChallenge.key_digest == digest)
            )
            if existing is not None:
                return await _view(db, existing)
            if isinstance(command, UUID):
                user = await db.scalar(
                    select(User)
                    .where(User.tenant_id == context.tenant_id, User.id == command)
                    .with_for_update()
                )
                if user is None:
                    raise NotFoundError()
            else:
                if await db.scalar(
                    select(User.id).where(
                        User.tenant_id == context.tenant_id,
                        func.lower(User.email) == command.email,
                    )
                ):
                    raise ConflictError()
                await grantable_role(db, context, command.role_id)
                account = await ensure_account(db, command.email, command.display_name)
                # Not active until the invited person proves they read this mailbox.
                user = User(
                    id=uuid4(),
                    tenant_id=context.tenant_id,
                    account_id=account.id,
                    email=command.email,
                    display_name=command.display_name,
                    role_id=command.role_id,
                    is_active=False,
                )
                db.add(user)
                await db.flush()
            challenge = await self.queue_invitation(db, user, digest)
            _team_audit(db, context, "team_member_invited", user.id)
            await db.flush()
            return await _view(db, challenge)

    async def _open(
        self, db: AsyncSession, token: str, *, lock: bool
    ) -> tuple[AuthChallenge, User, Account, Tenant]:
        """The invitation behind a link, with its membership, account and workspace."""
        identity = parse_token(token, INVITATION)
        await verify_runtime_role(db)
        query = select(AuthChallenge).where(AuthChallenge.id == identity)
        challenge = await db.scalar(query.with_for_update() if lock else query)
        if (
            not token_matches(token, challenge.token_digest if challenge else None)
            or challenge is None
            or challenge.purpose != "invitation"
            or challenge.consumed_at is not None
            or challenge.expires_at <= datetime.now(UTC)
            or challenge.tenant_id is None
        ):
            raise AuthenticationError()
        tenant = await _tenant_lock(db, challenge.tenant_id)
        user_query = select(User).where(User.id == challenge.user_id)
        account_query = select(Account).where(Account.id == challenge.account_id)
        user = await db.scalar(user_query.with_for_update() if lock else user_query)
        account = await db.scalar(account_query.with_for_update() if lock else account_query)
        if user is None or account is None or not account.is_active or user.accepted_at:
            raise AuthenticationError()
        return challenge, user, account, tenant

    async def details(self, command: LinkToken) -> InvitationDetails:
        """What the invitation page shows before the person accepts."""
        async with self._sessions.begin() as db:
            challenge, user, account, tenant = await self._open(
                db, command.token.get_secret_value(), lock=False
            )
            role = await db.get(TenantRole, user.role_id)
            return InvitationDetails(
                workspace_name=tenant.name,
                email=account.email,
                display_name=user.display_name,
                role_name=role.name if role else "",
                expires_at=challenge.expires_at,
                password_required=account.password_hash is None,
            )

    async def accept(self, command: AcceptInvitation, request_id: UUID) -> SessionView | None:
        """Activate the membership. A new account is signed in straight away."""
        password_hash = (
            await asyncio.to_thread(hash_password, command.password.get_secret_value())
            if command.password is not None
            else None
        )
        async with self._sessions.begin() as db:
            challenge, user, account, _ = await self._open(
                db, command.token.get_secret_value(), lock=True
            )
            new_account = account.password_hash is None
            if new_account != (password_hash is not None):
                # A new account must choose a password; an invitation never replaces one.
                raise ConflictError()
            if new_account:
                account.password_hash = password_hash
            now = datetime.now(UTC)
            user.is_active, user.accepted_at = True, now
            challenge.consumed_at = now
            await db.execute(
                update(AuthMail)
                .where(AuthMail.challenge_id == challenge.id)
                .values(ciphertext=None, status="cancelled")
            )
            db.add(
                AuditEvent(
                    tenant_id=user.tenant_id,
                    actor_id=user.id,
                    actor_type=ActorType.USER,
                    action="invitation_accepted",
                    resource_type="user",
                    resource_id=user.id,
                    request_id=request_id,
                )
            )
            if not new_account:
                return None
            row, token = issue_session(db, account, WORKSPACE_SESSION)
            row.tenant_id = user.tenant_id
            await db.flush()
            await record(db, account.id, "signed_in", request_id)
            return SessionView(access_token=token, expires_at=row.expires_at, next="workspace")

    async def manage_member(
        self, context: RequestContext, user_id: UUID, command: TeamMemberUpdate | None
    ) -> None:
        """Change a member's role or active state; with no change, just sign them out."""
        require_permission(context, Permission.USER_MANAGE)
        async with self._sessions.begin() as db:
            await _team_actor(db, context)
            target = await db.scalar(
                select(User)
                .where(User.tenant_id == context.tenant_id, User.id == user_id)
                .with_for_update()
            )
            if target is None:
                raise NotFoundError()
            if command:
                if command.is_active and not target.is_active and target.accepted_at is None:
                    # A pending invitation cannot be activated by editing the member.
                    raise ConflictError()
                await grantable_role(db, context, target.role_id)
                if command.role_id is not None and command.role_id != target.role_id:
                    require_permission(context, Permission.ROLE_ASSIGN)
                    await grantable_role(db, context, command.role_id)
                    target.role_id = command.role_id
                target.is_active = command.is_active
                await db.flush()
                await assert_custodian(db, context.tenant_id)
                if not command.is_active:
                    # A deactivated member's unanswered invitation must stop working too.
                    await cancel_challenges(db, purpose="invitation", user_id=user_id)
            await revoke_sessions(db, target.account_id, tenant_id=context.tenant_id)
            _team_audit(
                db,
                context,
                "team_member_updated" if command else "member_signed_out",
                user_id,
            )
