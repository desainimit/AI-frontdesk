from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, ForeignKey, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from app.auth.context import RequestContext
from app.auth.policies import require_tenant
from app.core.database.base import Base
from app.core.database.types import IdentityMixin, TimestampMixin


class TenantRecord(IdentityMixin, TimestampMixin, Base):
    __abstract__ = True
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, bool]:
        return {"eager_defaults": True}


class ScopedRepository[T: TenantRecord]:
    """Common persistence only; authorization and workflows remain in domain services."""

    def __init__(self, model: type[T], session: AsyncSession, context: RequestContext) -> None:
        self._model = model
        self._session = session
        self._context = context

    async def get(self, identity: UUID, *, lock: bool = False) -> T | None:
        query = select(self._model).where(
            self._model.tenant_id == self._context.tenant_id, self._model.id == identity
        )
        return await self._session.scalar(query.with_for_update() if lock else query)

    async def add(self, entity: T) -> T:
        require_tenant(self._context, entity.tenant_id)
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def flush(self) -> None:
        await self._session.flush()

    async def list(
        self, limit: int, offset: int, *conditions: ColumnElement[bool] | None
    ) -> Sequence[T]:
        filters = [condition for condition in conditions if condition is not None]
        return (
            await self._session.scalars(
                select(self._model)
                .where(self._model.tenant_id == self._context.tenant_id, *filters)
                .order_by(self._model.created_at, self._model.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
