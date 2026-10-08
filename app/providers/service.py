from uuid import UUID

from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.database.search import contains
from app.database.unit_of_work import UnitOfWork
from app.errors import NotFoundError
from app.providers.models import Provider
from app.providers.schemas import CreateProvider, ProviderView


class ProviderService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create(self, context: RequestContext, command: CreateProvider) -> ProviderView:
        require_permission(context, Permission.DIRECTORY_WRITE)
        async with self._uow.transaction(context) as tx:
            if await tx.locations.get(command.location_id) is None:
                raise NotFoundError()
            entity = await tx.providers.add(
                Provider(tenant_id=context.tenant_id, **command.model_dump())
            )
            AuditService(tx.audit).record(
                context, AuditAction.PROVIDER_CREATED, "provider", entity.id
            )
            return ProviderView.model_validate(entity)

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        q: str | None = None,
        location_id: UUID | None = None,
    ) -> list[ProviderView]:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            rows = await tx.providers.list(
                limit,
                offset,
                contains(q, Provider.display_name, Provider.specialty),
                Provider.location_id == location_id if location_id else None,
            )
            return [ProviderView.model_validate(row) for row in rows]
