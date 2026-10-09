import hmac
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActorType, RequestContext, Role
from app.auth.models import ApiCredential
from app.auth.tokens import digest_token, parse_locator
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import AuthenticationError
from app.tenants.models import Tenant
from app.users.models import User


class AuthenticationService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def authenticate(self, token: str, request_id: UUID) -> RequestContext:
        if token.startswith("fhu1."):
            from app.auth.human import authenticate_human

            return await authenticate_human(self._sessions, token, request_id)
        locator = parse_locator(token)
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            # This pre-auth scope permits only credential lookup, not domain operations.
            await set_tenant_scope(session, locator.tenant_id)
            credential = await session.scalar(
                select(ApiCredential).where(
                    ApiCredential.id == locator.credential_id,
                    ApiCredential.tenant_id == locator.tenant_id,
                )
            )
            expected = credential.token_digest if credential else "0" * 64
            matches = hmac.compare_digest(expected, digest_token(token))
            if (
                not matches
                or credential is None
                or credential.revoked_at is not None
                or credential.expires_at <= datetime.now(UTC)
            ):
                raise AuthenticationError()
            user = await session.scalar(
                select(User).where(
                    User.id == credential.user_id,
                    User.tenant_id == locator.tenant_id,
                    User.is_active.is_(True),
                )
            )
            tenant = await session.scalar(
                select(Tenant).where(
                    Tenant.id == locator.tenant_id,
                    Tenant.is_active.is_(True),
                )
            )
            if user is None or tenant is None:
                raise AuthenticationError()
            context = RequestContext(
                request_id=request_id,
                tenant_id=tenant.id,
                actor_id=user.id,
                actor_type=ActorType.USER,
                role=Role(user.role),
            )
            from app.rbac.service import bind_user_permissions

            return await bind_user_permissions(session, context, user)
