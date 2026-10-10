"""Appointment types and each provider's working week."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.clinic.models import ProviderSchedule, ProviderTimeOff, Service
from app.clinic.schemas import (
    ProviderScheduleView,
    ScheduleWindow,
    ServiceCommand,
    ServiceView,
    TimeOffCommand,
    TimeOffView,
    WeeklySchedule,
)
from app.core.database.unit_of_work import Transaction, UnitOfWork
from app.core.errors import ConflictError, NotFoundError


class NameTakenError(ConflictError):
    code = "name_taken"
    public_message = "Another one already has this name. Choose a different name."


class ClinicService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def list_services(
        self, context: RequestContext, limit: int, offset: int, *, active_only: bool = False
    ) -> list[ServiceView]:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            rows = await tx.schedule.services(limit, offset, active_only=active_only)
            return [ServiceView.model_validate(row) for row in rows]

    async def save_service(
        self, context: RequestContext, command: ServiceCommand, identity: UUID | None = None
    ) -> ServiceView:
        require_permission(context, Permission.DIRECTORY_WRITE)
        try:
            async with self._uow.transaction(context) as tx:
                if identity is None:
                    row = Service(tenant_id=context.tenant_id, **command.model_dump())
                    tx.schedule.add(row)
                else:
                    row = await tx.schedule.service(identity, lock=True)
                    if row is None:
                        raise NotFoundError()
                    for field, value in command.model_dump().items():
                        setattr(row, field, value)
                await tx.schedule.flush()
                AuditService(tx.audit).record(
                    context,
                    AuditAction.SERVICE_CREATED
                    if identity is None
                    else AuditAction.SERVICE_UPDATED,
                    "service",
                    row.id,
                )
                return ServiceView.model_validate(row)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise NameTakenError() from None
            raise

    async def schedule(self, context: RequestContext, provider_id: UUID) -> ProviderScheduleView:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            return await self._schedule(tx, provider_id)

    async def set_schedule(
        self, context: RequestContext, provider_id: UUID, command: WeeklySchedule
    ) -> ProviderScheduleView:
        """Replace the provider's whole working week."""
        require_permission(context, Permission.DIRECTORY_WRITE)
        async with self._uow.transaction(context) as tx:
            if await tx.providers.get(provider_id, lock=True) is None:
                raise NotFoundError()
            await tx.schedule.clear_windows(provider_id)
            for window in command.windows:
                tx.schedule.add(
                    ProviderSchedule(
                        tenant_id=context.tenant_id, provider_id=provider_id, **window.model_dump()
                    )
                )
            await tx.schedule.flush()
            AuditService(tx.audit).record(
                context, AuditAction.PROVIDER_SCHEDULE_UPDATED, "provider", provider_id
            )
            return await self._schedule(tx, provider_id)

    async def add_time_off(
        self, context: RequestContext, provider_id: UUID, command: TimeOffCommand
    ) -> TimeOffView:
        require_permission(context, Permission.DIRECTORY_WRITE)
        async with self._uow.transaction(context) as tx:
            if await tx.providers.get(provider_id) is None:
                raise NotFoundError()
            row = ProviderTimeOff(
                tenant_id=context.tenant_id, provider_id=provider_id, **command.model_dump()
            )
            tx.schedule.add(row)
            await tx.schedule.flush()
            AuditService(tx.audit).record(
                context, AuditAction.PROVIDER_SCHEDULE_UPDATED, "provider", provider_id
            )
            return TimeOffView.model_validate(row)

    async def remove_time_off(
        self, context: RequestContext, provider_id: UUID, identity: UUID
    ) -> None:
        require_permission(context, Permission.DIRECTORY_WRITE)
        async with self._uow.transaction(context) as tx:
            if not await tx.schedule.remove_time_off(provider_id, identity):
                raise NotFoundError()
            AuditService(tx.audit).record(
                context, AuditAction.PROVIDER_SCHEDULE_UPDATED, "provider", provider_id
            )

    @staticmethod
    async def _schedule(tx: Transaction, provider_id: UUID) -> ProviderScheduleView:
        timezone = await tx.schedule.timezone(provider_id)
        if timezone is None:
            raise NotFoundError()
        return ProviderScheduleView(
            provider_id=provider_id,
            timezone=timezone,
            windows=[
                ScheduleWindow(weekday=row.weekday, starts=row.starts, ends=row.ends)
                for row in await tx.schedule.windows(provider_id)
            ],
            time_off=[
                TimeOffView.model_validate(row) for row in await tx.schedule.time_off(provider_id)
            ],
        )
