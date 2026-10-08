"""Tenant password accounts, single-use challenges and revocable human sessions."""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext, Role
from app.auth.human_models import AuthChallenge, AuthMail, HumanAccount, HumanIdentity, HumanSession
from app.auth.human_schemas import (
    AcceptInvitation,
    EmailIdentity,
    HumanLogin,
    HumanSessionView,
    InvitationDetails,
    InvitationToken,
    InvitationView,
    InviteMember,
    ResetPassword,
    TeamMemberUpdate,
)
from app.auth.identity import identity_for_email, identity_scope
from app.auth.policies import Permission, require_permission
from app.auth.tokens import digest_token
from app.config.settings import Settings
from app.database.rls import set_tenant_scope, verify_runtime_role
from app.database.unit_of_work import enter_tenant_scope
from app.errors import AuthenticationError, AuthorizationError, ConflictError, NotFoundError
from app.events.idempotency import InvalidIdempotencyKeyError
from app.platform.models import PlatformAuditEvent
from app.platform.onboarding_models import TenantOnboarding
from app.platform.passwords import DUMMY_HASH, hash_password, verify_password
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.tenants import platform_scope
from app.platform.throttle import AuthenticationUnavailableError
from app.rbac.models import TenantRole
from app.rbac.service import assert_custodian, grantable_role, legacy_role
from app.security.encryption import SecretCipher
from app.tenants.models import Tenant
from app.users.models import User


def parse_human_token(token: str, prefix: str = "fhu1") -> tuple[UUID, UUID]:
    try:
        kind, tenant, identity, secret = token.split(".")
        if kind != prefix or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret) or len(token) > 160:
            raise ValueError
        return UUID(tenant), UUID(identity)
    except ValueError:
        raise AuthenticationError() from None


async def validate_human_context(session: AsyncSession, context: RequestContext) -> None:
    if context.human_session_id is None:
        raise AuthenticationError()
    active = await session.scalar(
        select(HumanSession.id).where(
            HumanSession.id == context.human_session_id,
            HumanSession.tenant_id == context.tenant_id,
            HumanSession.user_id == context.actor_id,
            HumanSession.revoked_at.is_(None),
            HumanSession.expires_at > datetime.now(UTC),
        )
    )
    if active is None:
        raise AuthenticationError()
    row = await session.get(HumanSession, context.human_session_id)
    user = await session.get(User, context.actor_id)
    if row is None or user is None:
        raise AuthenticationError()
    await identity_scope(session, user.email)
    identity = await session.scalar(
        select(HumanIdentity).where(HumanIdentity.id == row.identity_id).with_for_update(read=True)
    )
    if (
        identity is None
        or identity.password_hash is None
        or identity.credential_revision != row.credential_revision
    ):
        raise AuthenticationError()


async def authenticate_human(
    sessions: async_sessionmaker[AsyncSession], token: str, request_id: UUID
) -> RequestContext:
    tenant_id, session_id = parse_human_token(token)
    async with sessions.begin() as session:
        await verify_runtime_role(session)
        await set_tenant_scope(session, tenant_id)
        row = await session.get(HumanSession, session_id)
        matches = hmac.compare_digest(row.token_digest if row else "0" * 64, digest_token(token))
        if not matches or row is None or row.revoked_at or row.expires_at <= datetime.now(UTC):
            raise AuthenticationError()
        user = await session.get(User, row.user_id)
        tenant = await session.get(Tenant, tenant_id)
        if user is None or not user.is_active or tenant is None or not tenant.is_active:
            raise AuthenticationError()
        context = RequestContext(
            request_id,
            tenant_id,
            user.id,
            ActorType.USER,
            Role(user.role),
            human_session_id=session_id,
        )
        await validate_human_context(session, context)
        from app.rbac.service import bind_user_permissions

        return await bind_user_permissions(session, context, user)


def _audit(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, action: str, request_id: UUID
) -> None:
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user_id,
            actor_type=ActorType.USER,
            action=action,
            resource_type="user",
            resource_id=user_id,
            request_id=request_id,
        )
    )


def _team_audit(session: AsyncSession, context: RequestContext, action: str, user_id: UUID) -> None:
    session.add(
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


async def _team_actor(session: AsyncSession, context: RequestContext) -> None:
    """Serialize team changes per tenant, then recheck who is making this one.

    Open to the tenant's administrators and to a platform administrator inside a
    support session. The membership, or the support session, is re-read under the lock.
    """
    if context.actor_type not in {ActorType.USER, ActorType.PLATFORM_SUPPORT}:
        raise AuthorizationError()
    await _tenant_lock(session, context.tenant_id)
    await enter_tenant_scope(session, context)


async def _tenant_lock(session: AsyncSession, tenant_id: UUID) -> Tenant:
    await set_tenant_scope(session, tenant_id)
    tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
    if tenant is None or not tenant.is_active:
        raise AuthenticationError()
    return tenant


def issue_membership_session(
    session: AsyncSession, user: User, account: HumanIdentity
) -> HumanSessionView:
    identity = uuid4()
    token = f"fhu1.{user.tenant_id}.{identity}.{secrets.token_urlsafe(32)}"
    expires = datetime.now(UTC) + timedelta(hours=8)
    session.add(
        HumanSession(
            id=identity,
            tenant_id=user.tenant_id,
            user_id=user.id,
            identity_id=account.id,
            credential_revision=account.credential_revision,
            token_digest=digest_token(token),
            expires_at=expires,
        )
    )
    return HumanSessionView(access_token=token, tenant_id=user.tenant_id, expires_at=expires)


async def _revoke_sessions(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> None:
    await session.execute(
        update(HumanSession)
        .where(
            HumanSession.tenant_id == tenant_id,
            HumanSession.user_id == user_id,
            HumanSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def cancel_challenges(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, purpose: str
) -> None:
    """Invalidate outstanding challenges and erase their undelivered mail payloads."""
    ids = select(AuthChallenge.id).where(
        AuthChallenge.tenant_id == tenant_id,
        AuthChallenge.user_id == user_id,
        AuthChallenge.purpose == purpose,
        AuthChallenge.consumed_at.is_(None),
    )
    await session.execute(
        update(AuthMail)
        .where(AuthMail.tenant_id == tenant_id, AuthMail.challenge_id.in_(ids))
        .values(status="cancelled", ciphertext=None)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AuthChallenge)
        .where(AuthChallenge.tenant_id == tenant_id, AuthChallenge.id.in_(ids))
        .values(consumed_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )


async def membership_state(session: AsyncSession, user: User) -> tuple[str, str]:
    """Return (member status, invitation status) from current evidence, never tokens."""
    accepted = await session.scalar(
        select(HumanAccount.id).where(
            HumanAccount.tenant_id == user.tenant_id, HumanAccount.user_id == user.id
        )
    )
    if accepted is not None:
        return ("active" if user.is_active else "deactivated"), "accepted"
    row = (
        await session.execute(
            select(AuthChallenge.expires_at, AuthMail.status)
            .join(AuthMail, AuthMail.challenge_id == AuthChallenge.id, isouter=True)
            .where(
                AuthChallenge.tenant_id == user.tenant_id,
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


class HumanAuthenticationService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions, self.settings = sessions, settings

    def _enabled(self, *, mail: bool = False) -> None:
        if mail and not self.settings.account_mail_ready:
            raise AuthenticationUnavailableError()

    def _cipher(self) -> SecretCipher:
        return SecretCipher(
            self.settings.secret_encryption_keys, self.settings.secret_encryption_active_key
        )

    def _otp_digest(self, challenge: AuthChallenge, code: str) -> str:
        payload = f"reset:{challenge.tenant_id}:{challenge.user_id}:{challenge.id}:{code}"
        return hmac.new(
            self.settings.otp_hmac_key.get_secret_value().encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    async def _queue_mail(
        self,
        session: AsyncSession,
        challenge: AuthChallenge,
        recipient: str,
        subject: str,
        body: str,
    ) -> None:
        identity = uuid4()
        key_id, ciphertext = self._cipher().encrypt(
            json.dumps({"to": recipient, "subject": subject, "body": body}).encode(),
            f"auth-mail:{challenge.tenant_id}:{identity}",
        )
        await session.flush()
        session.add(
            AuthMail(
                id=identity,
                tenant_id=challenge.tenant_id,
                challenge_id=challenge.id,
                key_id=key_id,
                ciphertext=ciphertext,
                expires_at=challenge.expires_at,
                available_at=datetime.now(UTC),
            )
        )

    async def queue_invitation(
        self, session: AsyncSession, user: User, key_digest: str
    ) -> AuthChallenge:
        self._enabled(mail=True)
        if await session.scalar(select(HumanAccount.id).where(HumanAccount.user_id == user.id)):
            raise ConflictError()
        await cancel_challenges(session, user.tenant_id, user.id, "invitation")
        identity = uuid4()
        token = f"fhi1.{user.tenant_id}.{identity}.{secrets.token_urlsafe(32)}"
        challenge = AuthChallenge(
            id=identity,
            tenant_id=user.tenant_id,
            user_id=user.id,
            purpose="invitation",
            key_digest=key_digest,
            token_digest=digest_token(token),
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        session.add(challenge)
        origin = self.settings.tenant_auth_public_origin.rstrip("/")
        tenant = await session.get(Tenant, user.tenant_id)
        role = await session.get(TenantRole, user.role_id) if user.role_id is not None else None
        account = await identity_for_email(session, user.email)
        acceptance = (
            "Choose your password"
            if account is None
            else "Accept membership using your existing account"
        )
        # Fragment, not query: the bearer token is never sent to servers or proxy logs.
        url = f"{origin}/tenant-auth/accept#token={token}"
        await self._queue_mail(
            session,
            challenge,
            user.email,
            "Your AI Frontdesk invitation",
            f"Workspace: {tenant.name if tenant else user.tenant_id}\n"
            f"Role: {role.name if role else 'Unassigned'}\n"
            f"Login: {origin}/tenant-auth/login?workspace={user.tenant_id}\n"
            f"{acceptance} here:\n{url}\n"
            "This link expires in 24 hours. Ignore this email if you did not expect it.",
        )
        return challenge

    async def invite_initial_admin(
        self, context: PlatformContext, tenant_id: UUID, key: str
    ) -> InvitationView:
        self._enabled(mail=True)
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise InvalidIdempotencyKeyError()
        digest = digest_token(f"platform-invite:{context.user_id}:{tenant_id}:{key}")
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            await _tenant_lock(session, tenant_id)
            row = await session.scalar(
                select(TenantOnboarding).where(TenantOnboarding.tenant_id == tenant_id)
            )
            if row is None:
                raise NotFoundError()
            existing = await session.scalar(
                select(AuthChallenge).where(
                    AuthChallenge.tenant_id == tenant_id, AuthChallenge.key_digest == digest
                )
            )
            if existing is None:
                user = await session.get(User, row.admin_id)
                if user is None:
                    raise NotFoundError()
                existing = await self.queue_invitation(session, user, digest)
                row.status = "invited"
                row.revision += 1
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        request_id=context.request_id,
                        tenant_id=tenant_id,
                        resource_id=existing.id,
                        action="tenant_admin_invited",
                    )
                )
                await session.flush()
            return await self._invitation_view(session, existing)

    async def _invitation_view(
        self, session: AsyncSession, challenge: AuthChallenge
    ) -> InvitationView:
        mail = await session.scalar(select(AuthMail).where(AuthMail.challenge_id == challenge.id))
        return InvitationView(
            id=challenge.id,
            tenant_id=challenge.tenant_id,
            user_id=challenge.user_id,
            expires_at=challenge.expires_at,
            delivery_status=mail.status if mail else "unavailable",
            consumed=challenge.consumed_at is not None,
        )

    async def invite_member(
        self, context: RequestContext, command: InviteMember | UUID, key: str
    ) -> InvitationView:
        """Invite a new member, or resend to a pending one. Replays return the same invite."""
        self._enabled(mail=True)
        require_permission(context, Permission.USER_INVITE)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise InvalidIdempotencyKeyError()
        principal = context.actor_id or context.platform_user_id
        digest = digest_token(f"member-invite:{principal}:{context.tenant_id}:{key}")
        async with self._sessions.begin() as session:
            await _team_actor(session, context)
            existing = await session.scalar(
                select(AuthChallenge).where(
                    AuthChallenge.tenant_id == context.tenant_id,
                    AuthChallenge.key_digest == digest,
                )
            )
            if existing is not None:
                return await self._invitation_view(session, existing)
            if isinstance(command, UUID):
                user = await session.scalar(
                    select(User)
                    .where(User.tenant_id == context.tenant_id, User.id == command)
                    .with_for_update()
                )
                if user is None:
                    raise NotFoundError()
            else:
                if await session.scalar(
                    select(User.id).where(
                        User.tenant_id == context.tenant_id,
                        func.lower(User.email) == command.email,
                    )
                ):
                    raise ConflictError()
                # Inactive until the invitee proves mailbox control and sets a password.
                role_id = command.role_id or await legacy_role(
                    session, context.tenant_id, command.role
                )
                await grantable_role(session, context, role_id)
                user = User(
                    id=uuid4(),
                    tenant_id=context.tenant_id,
                    email=command.email,
                    display_name=command.display_name,
                    role=command.role,
                    role_id=role_id,
                    is_active=False,
                )
                session.add(user)
                await session.flush()
            challenge = await self.queue_invitation(session, user, digest)
            _team_audit(session, context, "team_member_invited", user.id)
            await session.flush()
            return await self._invitation_view(session, challenge)

    async def _login_candidate(
        self, session: AsyncSession, command: HumanLogin
    ) -> tuple[User, HumanIdentity] | None:
        await verify_runtime_role(session)
        await set_tenant_scope(session, command.tenant_id)
        row = (
            await session.execute(
                select(User, HumanAccount)
                .join(HumanAccount, HumanAccount.user_id == User.id)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(
                    User.tenant_id == command.tenant_id,
                    func.lower(User.email) == command.email,
                    User.is_active.is_(True),
                    Tenant.is_active.is_(True),
                )
            )
        ).all()
        if len(row) != 1:
            return None
        account = await identity_for_email(session, command.email)
        if account is None or account.id != row[0][1].identity_id or account.password_hash is None:
            return None
        return row[0][0], account

    async def login(self, command: HumanLogin, request_id: UUID) -> HumanSessionView:
        self._enabled()
        # No pooled connection or row lock is held while the password hash is computed.
        async with self._sessions.begin() as session:
            candidate = await self._login_candidate(session, command)
        expected = (
            candidate[1].password_hash if candidate and candidate[1].password_hash else DUMMY_HASH
        )
        valid = await asyncio.to_thread(
            verify_password, command.password.get_secret_value(), expected
        )
        if not valid or candidate is None:
            raise AuthenticationError()
        async with self._sessions.begin() as session:
            # Recheck: deactivation or a password reset may have raced the hash.
            current = await self._login_candidate(session, command)
            if (
                current is None
                or current[1].password_hash is None
                or not hmac.compare_digest(current[1].password_hash, expected)
            ):
                raise AuthenticationError()
            result = issue_membership_session(session, current[0], current[1])
            _audit(session, command.tenant_id, current[0].id, "human_login", request_id)
            return result

    async def invitation_details(self, command: InvitationToken) -> InvitationDetails:
        self._enabled()
        token = command.token.get_secret_value()
        tenant_id, identity = parse_human_token(token, "fhi1")
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, tenant_id)
            challenge = await session.get(AuthChallenge, identity)
            if (
                challenge is None
                or challenge.purpose != "invitation"
                or challenge.consumed_at
                or challenge.expires_at <= datetime.now(UTC)
                or not hmac.compare_digest(challenge.token_digest, digest_token(token))
            ):
                raise AuthenticationError()
            user = await session.get(User, challenge.user_id)
            tenant = await session.get(Tenant, tenant_id)
            if user is None or tenant is None or not tenant.is_active:
                raise AuthenticationError()
            existing = await identity_for_email(session, user.email)
            role = await session.get(TenantRole, user.role_id) if user.role_id is not None else None
            return InvitationDetails(
                tenant_id=tenant_id,
                business_name=tenant.name,
                display_name=user.display_name,
                role_name=role.name if role else "Unassigned",
                expires_at=challenge.expires_at,
                password_required=existing is None,
                account_requires_recovery=existing is not None and existing.password_hash is None,
            )

    async def accept(self, command: AcceptInvitation, request_id: UUID) -> None:
        self._enabled()
        token = command.token.get_secret_value()
        tenant_id, identity = parse_human_token(token, "fhi1")
        password_hash = (
            await asyncio.to_thread(hash_password, command.password.get_secret_value())
            if command.password is not None
            else None
        )
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await _tenant_lock(session, tenant_id)
            challenge = await session.get(AuthChallenge, identity)
            if (
                challenge is None
                or challenge.purpose != "invitation"
                or challenge.consumed_at
                or challenge.expires_at <= datetime.now(UTC)
                or not hmac.compare_digest(challenge.token_digest, digest_token(token))
            ):
                raise AuthenticationError()
            user = await session.scalar(
                select(User).where(User.id == challenge.user_id).with_for_update()
            )
            if user is None or await session.scalar(
                select(HumanAccount.id).where(HumanAccount.user_id == user.id)
            ):
                raise AuthenticationError()
            existing = await identity_for_email(session, user.email, lock=True)
            if existing is not None and existing.password_hash is not None:
                if password_hash is not None:
                    # A membership invitation never replaces an existing account password.
                    raise ConflictError()
            elif password_hash is None:
                raise ConflictError()
            elif existing is not None:
                # Conflicting legacy identities recover only through their reset flow.
                raise ConflictError()
            else:
                existing = HumanIdentity(
                    email=user.email.strip().lower(), password_hash=password_hash
                )
                session.add(existing)
                await session.flush()
            session.add(HumanAccount(tenant_id=tenant_id, user_id=user.id, identity_id=existing.id))
            user.is_active = True
            challenge.consumed_at = datetime.now(UTC)
            await session.execute(
                update(AuthMail)
                .where(AuthMail.challenge_id == identity)
                .values(ciphertext=None, status="cancelled")
            )
            # Onboarding reads derive acceptance from this membership; no platform RLS bypass.
            _audit(session, tenant_id, user.id, "invitation_accepted", request_id)

    async def request_reset(self, command: EmailIdentity, request_id: UUID) -> UUID | None:
        self._enabled(mail=True)
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, command.tenant_id)
            users = (
                await session.scalars(
                    select(User)
                    .join(HumanAccount, HumanAccount.user_id == User.id)
                    .join(Tenant, Tenant.id == User.tenant_id)
                    .where(
                        func.lower(User.email) == command.email,
                        User.is_active.is_(True),
                        Tenant.is_active.is_(True),
                    )
                    .order_by(User.id)
                    .with_for_update(of=User)
                )
            ).all()
            if len(users) != 1:
                return None  # Public response is identical; transport throttles all identities.
            user = users[0]
            account = await identity_for_email(session, user.email)
            if account is None:
                return None
            await cancel_challenges(session, user.tenant_id, user.id, "reset")
            code = f"{secrets.randbelow(100_000_000):08d}"
            challenge = AuthChallenge(
                id=uuid4(),
                tenant_id=user.tenant_id,
                user_id=user.id,
                purpose="reset",
                key_digest=digest_token(secrets.token_urlsafe(32)),
                token_digest="",
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
                credential_revision=account.credential_revision,
            )
            challenge.token_digest = self._otp_digest(challenge, code)
            session.add(challenge)
            await self._queue_mail(
                session,
                challenge,
                user.email,
                "AI Frontdesk password reset code",
                f"Your password reset code is {code}. It expires in 10 minutes.\n"
                f"Workspace: {user.tenant_id}\nIf you did not request this, ignore this email.",
            )
            _audit(session, user.tenant_id, user.id, "password_reset_requested", request_id)
            return challenge.id

    async def reset(
        self, command: ResetPassword, request_id: UUID, *, challenge_id: UUID | None = None
    ) -> None:
        self._enabled()
        password_hash = await asyncio.to_thread(hash_password, command.password.get_secret_value())
        success = False
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, command.tenant_id)
            users = (
                await session.scalars(
                    select(User)
                    .join(Tenant, Tenant.id == User.tenant_id)
                    .where(
                        func.lower(User.email) == command.email,
                        User.is_active.is_(True),
                        Tenant.is_active.is_(True),
                    )
                    .order_by(User.id)
                    .with_for_update(of=User)
                )
            ).all()
            if len(users) == 1:
                user = users[0]
                challenge = await session.scalar(
                    select(AuthChallenge)
                    .where(
                        AuthChallenge.user_id == user.id,
                        AuthChallenge.purpose == "reset",
                        AuthChallenge.consumed_at.is_(None),
                    )
                    .order_by(AuthChallenge.created_at.desc(), AuthChallenge.id.desc())
                    .limit(1)
                )
                membership = await session.scalar(
                    select(HumanAccount).where(HumanAccount.user_id == user.id)
                )
                account = await identity_for_email(session, user.email, lock=True)
                if (
                    challenge
                    and (challenge_id is None or challenge.id == challenge_id)
                    and account
                    and membership
                    and membership.identity_id == account.id
                    and challenge.credential_revision == account.credential_revision
                    and challenge.expires_at > datetime.now(UTC)
                    and challenge.attempts < 5
                ):
                    challenge.attempts += 1
                    success = hmac.compare_digest(
                        challenge.token_digest,
                        self._otp_digest(challenge, command.otp.get_secret_value()),
                    )
                    if success:
                        challenge.consumed_at = datetime.now(UTC)
                        account.password_hash = password_hash
                        account.credential_revision += 1
                        await _revoke_sessions(session, user.tenant_id, user.id)
                        await session.execute(
                            update(AuthMail)
                            .where(AuthMail.challenge_id == challenge.id)
                            .values(ciphertext=None, status="cancelled")
                        )
                        _audit(
                            session, user.tenant_id, user.id, "password_reset_completed", request_id
                        )
        # Commit failed attempts BEFORE raising the public error.
        if not success:
            raise AuthenticationError()

    async def logout(self, context: RequestContext) -> None:
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            await validate_human_context(session, context)
            await session.execute(
                update(HumanSession)
                .where(HumanSession.id == context.human_session_id)
                .values(revoked_at=datetime.now(UTC))
            )
            if context.actor_id:
                _audit(
                    session, context.tenant_id, context.actor_id, "human_logout", context.request_id
                )

    async def manage_member(
        self, context: RequestContext, user_id: UUID, command: TeamMemberUpdate | None
    ) -> None:
        require_permission(context, Permission.USER_MANAGE)
        async with self._sessions.begin() as session:
            await _team_actor(session, context)
            target = await session.scalar(
                select(User)
                .where(User.tenant_id == context.tenant_id, User.id == user_id)
                .with_for_update()
            )
            if target is None:
                raise NotFoundError()
            if command:
                account = await session.scalar(
                    select(HumanAccount.id).where(HumanAccount.user_id == user_id)
                )
                if command.is_active and not target.is_active and not account:
                    # A pending invitation cannot be activated by a role edit.
                    raise ConflictError()
                if target.role_id is not None:
                    await grantable_role(session, context, target.role_id)
                if command.role_id is not None or command.role != target.role:
                    require_permission(context, Permission.ROLE_ASSIGN)
                    role_id = command.role_id or await legacy_role(
                        session, context.tenant_id, command.role
                    )
                    await grantable_role(session, context, role_id)
                    target.role_id = role_id
                target.is_active, target.role = command.is_active, command.role
                await session.flush()
                await assert_custodian(session, context.tenant_id)
                if not command.is_active:
                    # A deactivated member's unanswered invitation must stop working too.
                    await cancel_challenges(session, context.tenant_id, user_id, "invitation")
            await _revoke_sessions(session, context.tenant_id, user_id)
            _team_audit(
                session,
                context,
                "team_member_updated" if command else "human_sessions_revoked",
                user_id,
            )
