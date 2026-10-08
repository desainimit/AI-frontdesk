from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.database.domain import ScopedRepository
from app.providers.models import Provider


class ProviderRepository(ScopedRepository[Provider]):
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        super().__init__(Provider, session, context)

    async def display_names(self, ids: set[UUID]) -> dict[UUID, str]:
        if not ids:
            return {}
        rows = await self._session.execute(
            select(Provider.id, Provider.display_name).where(
                Provider.tenant_id == self._context.tenant_id, Provider.id.in_(ids)
            )
        )
        return {row[0]: row[1] for row in rows}
