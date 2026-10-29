from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.core.database.domain import ScopedRepository
from app.providers.models import Provider


class ProviderRepository(ScopedRepository[Provider]):
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        super().__init__(Provider, session, context)

    async def follow_lines(self, provider: Provider) -> None:
        """A provider's number goes where they go: to their new location, or back to the
        whole clinic when they stop seeing patients."""
        from sqlalchemy import update

        from app.calls.models import PhoneNumber

        values = (
            {"location_id": provider.location_id}
            if provider.is_active
            else {"provider_id": None, "location_id": None}
        )
        await self._session.execute(
            update(PhoneNumber)
            .where(
                PhoneNumber.tenant_id == provider.tenant_id,
                PhoneNumber.provider_id == provider.id,
            )
            .values(**values)
        )

    async def display_names(self, ids: set[UUID]) -> dict[UUID, str]:
        if not ids:
            return {}
        rows = await self._session.execute(
            select(Provider.id, Provider.display_name).where(
                Provider.tenant_id == self._context.tenant_id, Provider.id.in_(ids)
            )
        )
        return {row[0]: row[1] for row in rows}
