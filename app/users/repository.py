from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.auth.human_models import HumanAccount
from app.database.search import contains
from app.rbac.models import TenantRole
from app.users.models import User


class UserRepository:
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session = session
        self._context = context

    async def get(self, user_id: UUID) -> User | None:
        return await self._session.scalar(
            select(User).where(User.tenant_id == self._context.tenant_id, User.id == user_id)
        )

    async def with_password(self, user_ids: Sequence[UUID]) -> set[UUID]:
        """Members who accepted an invitation and can sign in with a password."""
        if not user_ids:
            return set()
        return set(
            await self._session.scalars(
                select(HumanAccount.user_id).where(
                    HumanAccount.tenant_id == self._context.tenant_id,
                    HumanAccount.user_id.in_(user_ids),
                )
            )
        )

    async def role_names(self) -> dict[UUID, str]:
        rows = await self._session.execute(
            select(TenantRole.id, TenantRole.name).where(
                TenantRole.tenant_id == self._context.tenant_id
            )
        )
        return {identity: name for identity, name in rows}

    async def list(
        self,
        limit: int,
        offset: int,
        *,
        q: str | None = None,
        role: str | None = None,
        role_id: UUID | None = None,
        is_active: bool | None = None,
    ) -> Sequence[User]:
        query = select(User).where(User.tenant_id == self._context.tenant_id)
        search = contains(q, User.display_name, User.email)
        if search is not None:
            query = query.where(search)
        if role is not None:
            query = query.where(User.role == role)
        if role_id is not None:
            query = query.where(User.role_id == role_id)
        if is_active is not None:
            query = query.where(User.is_active.is_(is_active))
        return (
            await self._session.scalars(
                query.order_by(User.created_at, User.id).limit(limit).offset(offset)
            )
        ).all()
