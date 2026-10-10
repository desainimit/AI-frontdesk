from uuid import UUID

from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.clinic.models import ProviderSchedule
from app.core.database.search import contains
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import NotFoundError
from app.providers.models import Provider
from app.providers.schemas import CreateProvider, ProviderView, UpdateProvider


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
            # A new provider works the clinic's opening hours until someone changes it.
            for window in (await tx.schedule.settings()).hours.windows:
                tx.schedule.add(
                    ProviderSchedule(
                        tenant_id=context.tenant_id,
                        provider_id=entity.id,
                        weekday=window.weekday,
                        starts=window.opens,
                        ends=window.closes,
                    )
                )
            await tx.schedule.flush()
            AuditService(tx.audit).record(
                context, AuditAction.PROVIDER_CREATED, "provider", entity.id
            )
            return ProviderView.model_validate(entity)

    async def update(
        self, context: RequestContext, identity: UUID, command: UpdateProvider
    ) -> ProviderView:
        require_permission(context, Permission.DIRECTORY_WRITE)
        async with self._uow.transaction(context) as tx:
            entity = await tx.providers.get(identity, lock=True)
            if entity is None or await tx.locations.get(command.location_id) is None:
                raise NotFoundError()
            for field, value in command.model_dump().items():
                setattr(entity, field, value)
            await tx.providers.flush()
            AuditService(tx.audit).record(
                context, AuditAction.PROVIDER_UPDATED, "provider", entity.id
            )
            return ProviderView.model_validate(entity)

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        q: str | None = None,
        location_id: UUID | None = None,
        is_active: bool | None = None,
    ) -> list[ProviderView]:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            rows = await tx.providers.list(
                limit,
                offset,
                contains(q, Provider.display_name, Provider.specialty),
                Provider.location_id == location_id if location_id else None,
                Provider.is_active.is_(is_active) if is_active is not None else None,
            )
            return [ProviderView.model_validate(row) for row in rows]
