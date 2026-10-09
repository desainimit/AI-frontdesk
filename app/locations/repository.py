from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.core.database.domain import ScopedRepository
from app.locations.models import Location


class LocationRepository(ScopedRepository[Location]):
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        super().__init__(Location, session, context)
