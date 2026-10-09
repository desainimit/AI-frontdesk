"""Signing in and out, choosing a workspace, and changing or resetting a password."""

import asyncio
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import AuthenticationError, AuthorizationError, NotFoundError
from app.core.settings import Settings
from app.identity.mail import queue_mail
from app.identity.models import Account, AccountMfa, AccountSession, AuthChallenge, AuthMail
from app.identity.passwords import DUMMY_HASH, hash_password, verify_password
from app.identity.schemas import (
    AccountView,
    ChangePassword,
    CurrentWorkspace,
    ForgotPassword,
    Login,
    MeView,
    MfaProof,
    NextStep,
    OpenWorkspace,
    ResetPassword,
    SelectWorkspace,
    SessionView,
    SupportVisit,
    WorkspaceOption,
)
from app.identity.sessions import (
    PENDING_SESSION,
    SUPPORT_VISIT,
    WorkspaceRequiredError,
    account_scope,
    issue_session,
    load_session,
    platform_scope,
    session_lifetime,
    workspace_context,
)
from app.identity.throttle import AuthenticationUnavailableError
from app.identity.tokens import (
    RESET,
    SESSION,
    digest_token,
    issue_token,
    parse_token,
    token_matches,
)
from app.identity.totp import cipher, consume_code, consume_recovery
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.tenants.models import Tenant
from app.users.models import User

RESET_LINK = timedelta(minutes=30)


async def record(
    db: AsyncSession,
    account_id: UUID,
    action: str,
    request_id: UUID,
    *,
    tenant_id: UUID | None = None,
    support_id: UUID | None = None,
) -> None:
    """Add one account activity entry.

    Flushed one at a time: the API login may not read this table back, which a
    batched insert of several rows would need.
    """
    db.add(
        PlatformAuditEvent(
            user_id=account_id,
            action=action,
            request_id=request_id,
            tenant_id=tenant_id,
            support_session_id=support_id,
            resource_id=support_id,
        )
    )
    await db.flush()


async def cancel_challenges(
    db: AsyncSession, *, purpose: str, account_id: UUID | None = None, user_id: UUID | None = None
) -> None:
    """Invalidate outstanding links of one kind and erase their unsent email text."""
    owner = (
        AuthChallenge.user_id == user_id
        if user_id is not None
        else AuthChallenge.account_id == account_id
    )
    ids = select(AuthChallenge.id).where(
        owner, AuthChallenge.purpose == purpose, AuthChallenge.consumed_at.is_(None)
    )
    await db.execute(
        update(AuthMail)
        .where(AuthMail.challenge_id.in_(ids))
        .values(status="cancelled", ciphertext=None)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        update(AuthChallenge)
        .where(AuthChallenge.id.in_(ids))
        .values(consumed_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )


async def revoke_sessions(
    db: AsyncSession, account_id: UUID, *, keep: UUID | None = None, tenant_id: UUID | None = None
) -> None:
    """Sign the account out: everywhere, everywhere but one session, or of one workspace."""
    statement = update(AccountSession).where(
        AccountSession.account_id == account_id, AccountSession.revoked_at.is_(None)
    )
    if keep is not None:
        statement = statement.where(AccountSession.id != keep)
    if tenant_id is not None:
        statement = statement.where(
            AccountSession.tenant_id == tenant_id, AccountSession.support_id.is_(None)
        )
    await db.execute(
        statement.values(revoked_at=datetime.now(UTC)).execution_options(synchronize_session=False)
    )


class IdentityService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions, self._settings = sessions, settings

    # --- signing in

    async def login(self, command: Login, request_id: UUID) -> SessionView:
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            account = await db.scalar(select(Account).where(Account.email == command.email))
            usable = account is not None and account.is_active and bool(account.password_hash)
            expected = account.password_hash if account and account.password_hash else DUMMY_HASH
            account_id = account.id if account else None
        # No connection is held while the password hash is computed, and an unknown
        # address does the same work as a known one.
        valid = await asyncio.to_thread(
            verify_password, command.password.get_secret_value(), expected
        )
        if not valid or not usable or account_id is None:
            raise AuthenticationError()
        async with self._sessions.begin() as db:
            # Recheck: a deactivation or password change may have raced the hash.
            current = await db.get(Account, account_id)
            if current is None or not current.is_active or current.password_hash != expected:
                raise AuthenticationError()
            two_step = await db.scalar(
                select(AccountMfa.id).where(
                    AccountMfa.account_id == current.id, AccountMfa.enabled_at.is_not(None)
                )
            )
            if two_step is not None:
                row, token = issue_session(db, current, PENDING_SESSION, mfa_pending=True)
                return SessionView(access_token=token, expires_at=row.expires_at, next="mfa")
            row, token = issue_session(db, current, session_lifetime(self._settings, current))
            await db.flush()
            step = await self._enter(db, row, current, request_id)
            return SessionView(access_token=token, expires_at=row.expires_at, next=step)

    async def login_mfa(self, token: str, proof: MfaProof, request_id: UUID) -> SessionView:
        """Finish a sign-in that is waiting for the two-step code."""
        encryption = cipher(self._settings)
        result: SessionView | None = None
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            row, account = await load_session(db, token, pending=True, lock=True)
            factor = await db.scalar(
                select(AccountMfa).where(AccountMfa.account_id == account.id).with_for_update()
            )
            if factor is None or factor.enabled_at is None:
                raise AuthenticationError()
            now = datetime.now(UTC)
            accepted = (
                consume_recovery(factor, proof.recovery_code.get_secret_value(), now)
                if proof.recovery_code is not None
                else consume_code(
                    factor, encryption, proof.code.get_secret_value() if proof.code else "", now
                )
            )
            if accepted:
                row.mfa_pending, row.mfa_verified_at = False, now
                row.expires_at = now + session_lifetime(self._settings, account)
                if proof.recovery_code is not None:
                    # A recovery code means the phone may be lost: end every other session.
                    await revoke_sessions(db, account.id, keep=row.id)
                    await record(db, account.id, "recovery_code_used", request_id)
                step = await self._enter(db, row, account, request_id)
                result = SessionView(access_token=token, expires_at=row.expires_at, next=step)
            else:
                await record(db, account.id, "two_step_failed", request_id)
        # The failed attempt and any lock are committed before the error is raised.
        if result is None:
            raise AuthenticationError()
        return result

    async def _enter(
        self, db: AsyncSession, row: AccountSession, account: Account, request_id: UUID
    ) -> NextStep:
        """Record the sign-in and place the session: platform, its one workspace, or a choice."""
        await record(db, account.id, "signed_in", request_id)
        if account.is_platform_admin:
            return "platform"
        options = await self._memberships(db, account.id)
        if len(options) != 1:
            return "choose_workspace"
        await self._select(db, row, options[0][0], request_id)
        return "workspace"

    async def _memberships(self, db: AsyncSession, account_id: UUID) -> list[tuple[User, Tenant]]:
        """Workspaces the account may enter: accepted, active membership of an active workspace."""
        await account_scope(db, account_id)
        rows = await db.execute(
            select(User, Tenant)
            .join(Tenant, Tenant.id == User.tenant_id)
            .where(
                User.account_id == account_id,
                User.is_active.is_(True),
                User.accepted_at.is_not(None),
                Tenant.is_active.is_(True),
            )
            .order_by(Tenant.name, Tenant.id)
        )
        return [(user, tenant) for user, tenant in rows]

    async def _select(
        self, db: AsyncSession, row: AccountSession, user: User, request_id: UUID
    ) -> None:
        row.tenant_id = user.tenant_id
        row.support_id, row.support_reason, row.support_expires_at = None, None, None
        await set_tenant_scope(db, user.tenant_id)
        db.add(
            AuditEvent(
                tenant_id=user.tenant_id,
                actor_id=user.id,
                actor_type=ActorType.USER,
                action="workspace_entered",
                resource_type="user",
                resource_id=user.id,
                request_id=request_id,
            )
        )

    # --- the current session

    async def me(self, token: str, request_id: UUID) -> MeView:
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            row, account = await load_session(db, token)
            return await self._me(db, row, account, request_id)

    async def _me(
        self, db: AsyncSession, row: AccountSession, account: Account, request_id: UUID
    ) -> MeView:
        factor = await db.scalar(select(AccountMfa).where(AccountMfa.account_id == account.id))
        enabled = factor is not None and factor.enabled_at is not None
        try:
            context = await workspace_context(db, row, account, request_id)
        except WorkspaceRequiredError:
            context = None
        workspace = await self._workspace_view(db, row, context) if context else None
        step: NextStep = (
            "workspace"
            if workspace is not None
            else "platform"
            if account.is_platform_admin
            else "choose_workspace"
        )
        return MeView(
            account=AccountView(
                id=account.id,
                email=account.email,
                display_name=account.display_name,
                is_platform_admin=account.is_platform_admin,
                two_step_enabled=enabled,
                recovery_codes_remaining=len(factor.recovery_digests) if factor and enabled else 0,
            ),
            session_id=row.id,
            expires_at=row.expires_at,
            workspace=workspace,
            next=step,
        )

    @staticmethod
    async def _workspace_view(
        db: AsyncSession, row: AccountSession, context: RequestContext
    ) -> CurrentWorkspace:
        tenant = await db.get(Tenant, context.tenant_id)
        if tenant is None:
            raise WorkspaceRequiredError()
        visiting = context.actor_type == ActorType.PLATFORM_SUPPORT
        support = (
            SupportVisit(
                id=row.support_id,
                reason=row.support_reason or "",
                expires_at=row.support_expires_at,
            )
            if visiting and row.support_id and row.support_expires_at
            else None
        )
        return CurrentWorkspace(
            id=tenant.id,
            name=tenant.name,
            service_state=tenant.service_state,
            user_id=context.actor_id,
            role_id=context.role_id,
            role_name="Platform administrator" if visiting else context.role_name or "",
            authorization_revision=context.authorization_revision,
            permissions=sorted(context.capabilities),
            support=support,
        )

    async def workspaces(self, token: str) -> list[WorkspaceOption]:
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            _, account = await load_session(db, token)
            return [
                WorkspaceOption(id=tenant.id, name=tenant.name, service_state=tenant.service_state)
                for _, tenant in await self._memberships(db, account.id)
            ]

    async def select_workspace(
        self, token: str, command: SelectWorkspace, request_id: UUID
    ) -> MeView:
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            row, account = await load_session(db, token, lock=True)
            chosen = next(
                (
                    user
                    for user, tenant in await self._memberships(db, account.id)
                    if tenant.id == command.workspace_id
                ),
                None,
            )
            if chosen is None:
                raise AuthorizationError()
            await self._select(db, row, chosen, request_id)
            await db.flush()
            return await self._me(db, row, account, request_id)

    async def open_workspace(
        self, context: PlatformContext, tenant_id: UUID, command: OpenWorkspace
    ) -> SupportVisit:
        """A platform administrator enters a workspace for a stated reason and a short time."""
        require_platform_permission(context, PlatformPermission.SUPPORT_ENTER)
        async with self._sessions.begin() as db:
            await platform_scope(db, context)
            tenant = await db.scalar(
                select(Tenant.id).where(Tenant.id == tenant_id, Tenant.is_active.is_(True))
            )
            row = await db.scalar(
                select(AccountSession)
                .where(AccountSession.id == context.session_id)
                .with_for_update()
            )
            if tenant is None or row is None:
                raise NotFoundError()
            # Never outlasts the sign-in itself.
            expires = min(datetime.now(UTC) + SUPPORT_VISIT, row.expires_at)
            visit = uuid4()
            row.tenant_id, row.support_id = tenant, visit
            row.support_reason, row.support_expires_at = command.reason, expires
            await record(
                db,
                context.user_id,
                "support_enter",
                context.request_id,
                tenant_id=tenant,
                support_id=visit,
            )
            return SupportVisit(id=visit, reason=command.reason, expires_at=expires)

    async def leave_workspace(self, token: str, request_id: UUID) -> MeView:
        """Step out of the current workspace; the sign-in itself continues."""
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            row, account = await load_session(db, token, lock=True)
            if row.support_id is not None:
                await record(
                    db,
                    account.id,
                    "support_exit",
                    request_id,
                    tenant_id=row.tenant_id,
                    support_id=row.support_id,
                )
            row.tenant_id = None
            row.support_id, row.support_reason, row.support_expires_at = None, None, None
            await db.flush()
            return await self._me(db, row, account, request_id)

    async def logout(self, token: str, request_id: UUID) -> None:
        """End the session. Works for one still waiting for its code, and is safe to repeat."""
        identity = parse_token(token, SESSION)
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            row = await db.scalar(
                select(AccountSession).where(AccountSession.id == identity).with_for_update()
            )
            if row is None or not token_matches(token, row.token_digest):
                raise AuthenticationError()
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                await record(db, row.account_id, "signed_out", request_id)

    # --- passwords

    async def change_password(self, token: str, command: ChangePassword, request_id: UUID) -> None:
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            _, account = await load_session(db, token)
            expected = account.password_hash or DUMMY_HASH
        valid = await asyncio.to_thread(
            verify_password, command.current_password.get_secret_value(), expected
        )
        new_hash = await asyncio.to_thread(hash_password, command.password.get_secret_value())
        if not valid:
            raise AuthenticationError()
        async with self._sessions.begin() as db:
            row, _ = await load_session(db, token, lock=True)
            account = await db.scalar(
                select(Account).where(Account.id == row.account_id).with_for_update()
            )
            if account is None or account.password_hash != expected:
                raise AuthenticationError()
            account.password_hash = new_hash
            account.credential_revision += 1
            # This browser stays signed in; every other session stops on its next request.
            row.credential_revision = account.credential_revision
            await cancel_challenges(db, purpose="reset", account_id=account.id)
            await record(db, account.id, "password_changed", request_id)

    async def forgot_password(self, command: ForgotPassword, request_id: UUID) -> None:
        """Email a reset link if the address has an account; the caller learns nothing."""
        if not self._settings.account_mail_ready:
            raise AuthenticationUnavailableError()
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            account = await db.scalar(
                select(Account).where(Account.email == command.email).with_for_update()
            )
            if account is None or not account.is_active or account.password_hash is None:
                return
            await cancel_challenges(db, purpose="reset", account_id=account.id)
            identity, token, digest = issue_token(RESET)
            challenge = AuthChallenge(
                id=identity,
                account_id=account.id,
                purpose="reset",
                key_digest=digest_token(secrets.token_urlsafe(32)),
                token_digest=digest,
                expires_at=datetime.now(UTC) + RESET_LINK,
                credential_revision=account.credential_revision,
            )
            db.add(challenge)
            origin = self._settings.tenant_auth_public_origin.rstrip("/")
            await queue_mail(
                db,
                self._settings,
                challenge,
                account.email,
                "Reset your AI Frontdesk password",
                f"Hello {account.display_name},\n\n"
                "Use this link to choose a new password. It works once and expires in "
                "30 minutes:\n"
                # Fragment, not query: the token is never sent to servers or kept in logs.
                f"{origin}/reset-password#token={token}\n\n"
                "If you did not ask for this, you can ignore this email.",
            )
            await record(db, account.id, "password_reset_requested", request_id)

    async def reset_password(self, command: ResetPassword, request_id: UUID) -> None:
        token = command.token.get_secret_value()
        identity = parse_token(token, RESET)
        new_hash = await asyncio.to_thread(hash_password, command.password.get_secret_value())
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            challenge = await db.scalar(
                select(AuthChallenge).where(AuthChallenge.id == identity).with_for_update()
            )
            account = (
                await db.scalar(
                    select(Account).where(Account.id == challenge.account_id).with_for_update()
                )
                if challenge is not None
                else None
            )
            if (
                not token_matches(token, challenge.token_digest if challenge else None)
                or challenge is None
                or account is None
                or challenge.purpose != "reset"
                or challenge.consumed_at is not None
                or challenge.expires_at <= datetime.now(UTC)
                or not account.is_active
                or challenge.credential_revision != account.credential_revision
            ):
                raise AuthenticationError()
            challenge.consumed_at = datetime.now(UTC)
            account.password_hash = new_hash
            account.credential_revision += 1
            await revoke_sessions(db, account.id)
            await db.execute(
                update(AuthMail)
                .where(AuthMail.challenge_id == challenge.id)
                .values(ciphertext=None, status="cancelled")
            )
            await record(db, account.id, "password_reset_completed", request_id)
