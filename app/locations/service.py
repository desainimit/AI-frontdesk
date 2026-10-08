from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.database.search import contains
from app.database.unit_of_work import UnitOfWork
from app.locations.models import Location
from app.locations.schemas import CreateLocation, LocationView


class LocationService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create(self, context: RequestContext, command: CreateLocation) -> LocationView:
        require_permission(context, Permission.DIRECTORY_WRITE)
        async with self._uow.transaction(context) as tx:
            entity = await tx.locations.add(
                Location(tenant_id=context.tenant_id, **command.model_dump())
            )
            AuditService(tx.audit).record(
                context, AuditAction.LOCATION_CREATED, "location", entity.id
            )
            return LocationView.model_validate(entity)

    async def list(
        self, context: RequestContext, limit: int, offset: int, q: str | None = None
    ) -> list[LocationView]:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            rows = await tx.locations.list(limit, offset, contains(q, Location.name))
            return [LocationView.model_validate(row) for row in rows]
