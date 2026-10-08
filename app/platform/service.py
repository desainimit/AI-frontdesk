import asyncio
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, true, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.tokens import digest_token
from app.config.settings import Settings
from app.database.rls import verify_runtime_role
from app.errors import AuthenticationError
from app.platform.models import (
    PlatformAuditEvent,
    PlatformAuthenticator,
    PlatformSession,
    PlatformUser,
)
from app.platform.passwords import DUMMY_HASH, verify_password
from app.platform.policies import (
    PlatformContext,
    PlatformPermission,
    PlatformRole,
    require_platform_permission,
)
from app.platform.schemas import LoginCommand, PlatformUserView, SessionView


class PlatformAuthenticationService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], settings: Settings | None = None
    ) -> None:
        self._sessions = sessions
        self._settings = settings

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        return self._sessions

    async def login(
        self, command: LoginCommand, request_id: UUID, session_minutes: int = 30
    ) -> SessionView:
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            user = await session.scalar(
                select(PlatformUser).where(PlatformUser.email == command.email)
            )
            expected = user.password_hash if user else DUMMY_HASH
        valid = await asyncio.to_thread(
            verify_password, command.password.get_secret_value(), expected
        )
        if not valid or user is None or not user.is_active or user.role != PlatformRole.SUPER_ADMIN:
            raise AuthenticationError()
        result: SessionView | None = None
        async with self._sessions.begin() as session:
            current = await session.get(PlatformUser, user.id)
            if current is None or not current.is_active or current.password_hash != expected:
                raise AuthenticationError()
            factor = await session.scalar(
                select(PlatformAuthenticator)
                .where(PlatformAuthenticator.user_id == user.id)
                .with_for_update()
            )
            verified = None
            can_issue = True
            if factor is not None and factor.enabled_at is not None:
                from app.platform.mfa import cipher, consume_code, consume_recovery

                now = datetime.now(UTC)
                accepted = (
                    consume_recovery(factor, command.recovery_code.get_secret_value(), now)
                    if command.recovery_code
                    else consume_code(
                        factor,
                        cipher(self._settings or Settings.from_environment()),
                        command.otp.get_secret_value() if command.otp else "",
                        now,
                    )
                )
                if accepted:
                    verified = now
                    if command.recovery_code:
                        await session.execute(
                            update(PlatformSession)
                            .where(
                                PlatformSession.user_id == user.id,
                                PlatformSession.revoked_at.is_(None),
                            )
                            .values(revoked_at=now)
                        )
                        session.add(
                            PlatformAuditEvent(
                                user_id=user.id,
                                request_id=request_id,
                                action="platform_recovery_code_used",
                            )
                        )
                        # Flush this immutable event before _issue adds the login event.
                        # Batched ORM RETURNING requests audit IDs, which are deliberately
                        # unreadable to the runtime role.
                        await session.flush()
                else:
                    # Persist failures and account lockout without issuing a session.
                    await session.flush()
                    can_issue = False
            if not can_issue:
                session.add(
                    PlatformAuditEvent(
                        user_id=user.id, request_id=request_id, action="super_admin_mfa_failed"
                    )
                )
            else:
                result = self._issue(
                    session,
                    user,
                    request_id,
                    session_minutes,
                    verified,
                    recovered=bool(command.recovery_code and verified),
                )
        if result is None:
            raise AuthenticationError()
        return result

    @staticmethod
    def _issue(
        session: AsyncSession,
        user: PlatformUser,
        request_id: UUID,
        session_minutes: int,
        verified: datetime | None,
        *,
        recovered: bool = False,
    ) -> SessionView:
        session_id = uuid4()
        token = f"fps1.{session_id}.{secrets.token_urlsafe(32)}"
        expires = datetime.now(UTC) + timedelta(minutes=session_minutes)
        session.add(
            PlatformSession(
                id=session_id,
                user_id=user.id,
                token_digest=digest_token(token),
                expires_at=expires,
                mfa_verified_at=verified,
            )
        )
        session.add(
            PlatformAuditEvent(user_id=user.id, action="super_admin_login", request_id=request_id)
        )
        return SessionView(
            access_token=token,
            expires_at=expires,
            mfa_verified=verified is not None,
            mfa_recovered=recovered,
        )

    async def authenticate(self, token: str, request_id: UUID) -> PlatformContext:
        try:
            prefix, identity, secret = token.split(".")
            if prefix != "fps1" or len(secret) != 43 or len(token) > 100:
                raise ValueError
            session_id = UUID(identity)
        except ValueError:
            raise AuthenticationError() from None
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            credential = await session.get(PlatformSession, session_id)
            matches = hmac.compare_digest(
                credential.token_digest if credential else "0" * 64, digest_token(token)
            )
            if (
                not matches
                or credential is None
                or credential.revoked_at is not None
                or credential.expires_at <= datetime.now(UTC)
            ):
                raise AuthenticationError()
            user = await session.get(PlatformUser, credential.user_id)
            if user is None or not user.is_active or user.role != PlatformRole.SUPER_ADMIN:
                raise AuthenticationError()
            return PlatformContext(
                user.id,
                credential.id,
                request_id,
                PlatformRole(user.role),
                mfa_verified=credential.mfa_verified_at is not None,
            )

    async def me(self, context: PlatformContext) -> PlatformUserView:
        require_platform_permission(context, PlatformPermission.IDENTITY_READ)
        async with self._sessions.begin() as session:
            user = await validate_context(session, context, require_mfa=False)
            credential = await session.get(PlatformSession, context.session_id)
            return PlatformUserView.model_validate(user).model_copy(
                update={
                    "mfa_verified": credential is not None
                    and credential.mfa_verified_at is not None,
                }
            )

    async def logout(self, context: PlatformContext) -> None:
        async with self._sessions.begin() as session:
            await validate_context(session, context, require_mfa=False)
            credential = await session.scalar(
                select(PlatformSession)
                .where(PlatformSession.id == context.session_id)
                .with_for_update()
            )
            if credential is None:
                raise AuthenticationError()
            if credential.revoked_at is None:
                credential.revoked_at = datetime.now(UTC)
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        action="super_admin_logout",
                        request_id=context.request_id,
                    )
                )


async def validate_context(
    session: AsyncSession, context: PlatformContext, *, require_mfa: bool = True
) -> PlatformUser:
    """Recheck persisted identity and session within the authorized operation."""
    await verify_runtime_role(session)
    user = await session.scalar(
        select(PlatformUser)
        .join(PlatformSession, PlatformSession.user_id == PlatformUser.id)
        .where(
            PlatformUser.id == context.user_id,
            PlatformUser.is_active.is_(True),
            PlatformUser.role == context.role,
            PlatformSession.id == context.session_id,
            PlatformSession.revoked_at.is_(None),
            PlatformSession.expires_at > datetime.now(UTC),
            PlatformSession.mfa_verified_at.is_not(None) if require_mfa else true(),
        )
    )
    if user is None:
        raise AuthenticationError()
    return user
