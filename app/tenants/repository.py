from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.tenants.models import Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session = session
        self._context = context

    async def current(self) -> Tenant | None:
        return await self._session.scalar(
            select(Tenant).where(Tenant.id == self._context.tenant_id)
        )
