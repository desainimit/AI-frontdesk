from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.appointments.commands import DefineAvailability, SlotQuery
from app.appointments.errors import PastTimeError, ScheduleOverlapError
from app.appointments.models import Availability
from app.appointments.schemas import AvailabilityView, Slot
from app.appointments.slots import available_slots
from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.database.unit_of_work import UnitOfWork
from app.errors import NotFoundError


class AvailabilityService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def define(
        self,
        context: RequestContext,
        provider_id: UUID,
        command: DefineAvailability,
    ) -> AvailabilityView:
        require_permission(context, Permission.DIRECTORY_WRITE)
        if command.starts_at <= datetime.now(UTC):
            raise PastTimeError()
        try:
            async with self._uow.transaction(context) as tx:
                if await tx.providers.get(provider_id) is None:
                    raise NotFoundError()
                entity = await tx.availability.add(
                    Availability(
                        tenant_id=context.tenant_id,
                        provider_id=provider_id,
                        **command.model_dump(),
                    )
                )
                AuditService(tx.audit).record(
                    context, AuditAction.AVAILABILITY_DEFINED, "availability", entity.id
                )
                return AvailabilityView.model_validate(entity)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23P01":
                raise ScheduleOverlapError() from None
            raise

    async def list_windows(
        self,
        context: RequestContext,
        provider_id: UUID,
        query: SlotQuery,
    ) -> list[AvailabilityView]:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            if await tx.providers.get(provider_id) is None:
                raise NotFoundError()
            return [
                AvailabilityView.model_validate(row)
                for row in await tx.availability.between(
                    provider_id, query.starts_at, query.ends_at
                )
            ]

    async def slots(
        self, context: RequestContext, provider_id: UUID, query: SlotQuery
    ) -> list[Slot]:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            if await tx.providers.get(provider_id) is None:
                raise NotFoundError()
            windows = await tx.availability.between(provider_id, query.starts_at, query.ends_at)
            occupied = await tx.appointments.between(provider_id, query.starts_at, query.ends_at)
            return available_slots(windows, occupied, query, datetime.now(UTC))
