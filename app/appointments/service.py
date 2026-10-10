from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.appointments.availability import appointment_minutes, booking_window
from app.appointments.commands import BookAppointment, CancelAppointment, RescheduleAppointment
from app.appointments.errors import (
    AppointmentStateError,
    PastTimeError,
    SlotUnavailableError,
    StaleAppointmentError,
    TooLateToChangeError,
)
from app.appointments.models import Appointment
from app.appointments.schemas import AppointmentView
from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, has_permission, require_permission
from app.calendar_sync.service import CalendarService
from app.core.database.unit_of_work import Transaction, UnitOfWork
from app.core.errors import ApplicationError, AuthorizationError, NotFoundError
from app.events.outbox import EventType
from app.notifications.appointments import queue_appointment_messages


class AppointmentService:
    def __init__(self, uow: UnitOfWork, calendar: CalendarService | None = None) -> None:
        self._uow = uow
        # When given, the provider's own calendar is asked once more just before booking.
        self._calendar = calendar

    async def _check_calendar_now(
        self,
        context: RequestContext,
        provider_id: UUID,
        service_id: UUID | None,
        starts_at: datetime,
    ) -> None:
        """Refuse a time the provider's calendar has taken since the last sync."""
        if self._calendar is None:
            return
        try:
            async with self._uow.transaction(context) as tx:
                end = await self._end(context, tx, provider_id, service_id, starts_at)
        except ApplicationError:
            # Not bookable for another reason; the booking itself says which.
            return
        if await self._calendar.busy_now(context.tenant_id, provider_id, starts_at, end):
            raise SlotUnavailableError()

    async def book(
        self, context: RequestContext, command: BookAppointment, idempotency_key: str | None = None
    ) -> AppointmentView:
        require_permission(context, Permission.APPOINTMENT_WRITE)
        if context.actor_type == ActorType.CALLER and command.patient_id != context.patient_id:
            raise AuthorizationError()
        if command.starts_at > datetime.now(UTC):
            await self._check_calendar_now(
                context, command.provider_id, command.service_id, command.starts_at
            )
        try:
            async with self._uow.transaction(context) as tx:
                replay = await tx.idempotency.replay(
                    idempotency_key, "appointment.book", command, AppointmentView
                )
                if replay is not None:
                    return replay
                if command.starts_at <= datetime.now(UTC):
                    raise PastTimeError()
                if (
                    await tx.providers.get(command.provider_id) is None
                    or await tx.patients.get(command.patient_id) is None
                ):
                    raise NotFoundError()
                end = await self._end(
                    context, tx, command.provider_id, command.service_id, command.starts_at
                )
                entity = await tx.appointments.add(
                    Appointment(
                        tenant_id=context.tenant_id,
                        provider_id=command.provider_id,
                        patient_id=command.patient_id,
                        service_id=command.service_id,
                        starts_at=command.starts_at,
                        ends_at=end,
                        status="booked",
                        version=1,
                    )
                )
                AuditService(tx.audit).record(
                    context, AuditAction.APPOINTMENT_CREATED, "appointment", entity.id
                )
                result = AppointmentView.model_validate(entity)
                tx.outbox.appointment(
                    EventType.APPOINTMENT_CREATED, await self._for_calendar(tx, entity)
                )
                await queue_appointment_messages(tx, entity, "booked")
                return tx.idempotency.save(result)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23P01":
                raise SlotUnavailableError() from None
            raise

    async def get(self, context: RequestContext, appointment_id: UUID) -> AppointmentView:
        require_permission(context, Permission.APPOINTMENT_READ)
        async with self._uow.transaction(context) as tx:
            entity = await self._get(tx, appointment_id)
            AuditService(tx.audit).record(
                context, AuditAction.APPOINTMENT_VIEWED, "appointment", entity.id
            )
            return (await self._views(context, tx, [entity]))[0]

    async def _views(
        self, context: RequestContext, tx: Transaction, entities: Sequence[Appointment]
    ) -> list[AppointmentView]:
        providers = await tx.providers.display_names({row.provider_id for row in entities})
        patients: dict[UUID, str] = {}
        if has_permission(context, Permission.PATIENT_READ):
            patients = await tx.patients.display_names({row.patient_id for row in entities})
        services = await tx.schedule.service_names(
            {row.service_id for row in entities if row.service_id}
        )
        return [
            AppointmentView.model_validate(row).model_copy(
                update={
                    "provider_name": providers.get(row.provider_id),
                    "patient_name": patients.get(row.patient_id),
                    "service_name": services.get(row.service_id) if row.service_id else None,
                }
            )
            for row in entities
        ]

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        status: str | None = None,
        provider_id: UUID | None = None,
        patient_id: UUID | None = None,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        newest_first: bool = False,
    ) -> list[AppointmentView]:
        require_permission(context, Permission.APPOINTMENT_READ)
        async with self._uow.transaction(context) as tx:
            entities = await tx.appointments.search(
                limit,
                offset,
                status=status,
                provider_id=provider_id,
                patient_id=patient_id,
                starts_from=starts_from,
                starts_to=starts_to,
                newest_first=newest_first,
            )
            AuditService(tx.audit).record(context, AuditAction.APPOINTMENTS_VIEWED, "appointments")
            return await self._views(context, tx, entities)

    async def cancel(
        self,
        context: RequestContext,
        appointment_id: UUID,
        command: CancelAppointment,
        idempotency_key: str | None = None,
    ) -> AppointmentView:
        require_permission(context, Permission.APPOINTMENT_WRITE)
        async with self._uow.transaction(context) as tx:
            replay = await tx.idempotency.replay(
                idempotency_key, "appointment.cancel", command, AppointmentView, appointment_id
            )
            if replay is not None:
                return replay
            entity = await self._get(tx, appointment_id, lock=True)
            if entity.status == "cancelled":
                return tx.idempotency.save(AppointmentView.model_validate(entity))
            self._require_mutable(entity, command.expected_version)
            await self._require_notice(context, tx, entity)
            entity.status = "cancelled"
            entity.version += 1
            await tx.appointments.flush()
            AuditService(tx.audit).record(
                context, AuditAction.APPOINTMENT_CANCELLED, "appointment", entity.id
            )
            result = AppointmentView.model_validate(entity)
            tx.outbox.appointment(
                EventType.APPOINTMENT_CANCELLED, await self._for_calendar(tx, entity)
            )
            await queue_appointment_messages(tx, entity, "cancelled")
            return tx.idempotency.save(result)

    async def reschedule(
        self,
        context: RequestContext,
        appointment_id: UUID,
        command: RescheduleAppointment,
        idempotency_key: str | None = None,
    ) -> AppointmentView:
        require_permission(context, Permission.APPOINTMENT_WRITE)
        if command.starts_at > datetime.now(UTC) and self._calendar is not None:
            # The calendar is asked before the change is made, outside its transaction.
            async with self._uow.transaction(context) as tx:
                current = await self._get(tx, appointment_id)
            await self._check_calendar_now(
                context, current.provider_id, current.service_id, command.starts_at
            )
        try:
            async with self._uow.transaction(context) as tx:
                replay = await tx.idempotency.replay(
                    idempotency_key,
                    "appointment.reschedule",
                    command,
                    AppointmentView,
                    appointment_id,
                )
                if replay is not None:
                    return replay
                if command.starts_at <= datetime.now(UTC):
                    raise PastTimeError()
                entity = await self._get(tx, appointment_id, lock=True)
                self._require_mutable(entity, command.expected_version)
                await self._require_notice(context, tx, entity, rescheduling=True)
                end = await self._end(
                    context, tx, entity.provider_id, entity.service_id, command.starts_at
                )
                if entity.starts_at == command.starts_at and entity.ends_at == end:
                    return tx.idempotency.save(AppointmentView.model_validate(entity))
                entity.starts_at, entity.ends_at = command.starts_at, end
                entity.version += 1
                await tx.appointments.flush()
                AuditService(tx.audit).record(
                    context, AuditAction.APPOINTMENT_RESCHEDULED, "appointment", entity.id
                )
                result = AppointmentView.model_validate(entity)
                tx.outbox.appointment(
                    EventType.APPOINTMENT_RESCHEDULED, await self._for_calendar(tx, entity)
                )
                await queue_appointment_messages(tx, entity, "rescheduled")
                return tx.idempotency.save(result)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23P01":
                raise SlotUnavailableError() from None
            raise

    @staticmethod
    async def _end(
        context: RequestContext,
        tx: Transaction,
        provider_id: UUID,
        service_id: UUID | None,
        starts_at: datetime,
    ) -> datetime:
        """When an appointment starting here ends, once the schedule and rules allow it."""
        settings = await tx.schedule.settings()
        calendar = await tx.schedule.calendar(provider_id, settings)
        if calendar is None:
            raise SlotUnavailableError()
        earliest, latest = booking_window(context, settings, datetime.now(UTC))
        if starts_at < earliest or (latest is not None and starts_at > latest):
            raise SlotUnavailableError()
        ends_at = calendar.require_bookable(
            starts_at, await appointment_minutes(tx, settings, service_id)
        )
        if await tx.schedule.calendar_busy(provider_id, starts_at, ends_at):
            raise SlotUnavailableError()
        return ends_at

    @staticmethod
    async def _for_calendar(tx: Transaction, entity: Appointment) -> AppointmentView:
        """The appointment as the worker writes it to a calendar.

        The patient's name is included only when the clinic chose to show it there.
        """
        view = AppointmentView.model_validate(entity)
        names: dict[str, str | None] = {}
        if entity.service_id is not None:
            names["service_name"] = (await tx.schedule.service_names({entity.service_id})).get(
                entity.service_id
            )
        if (await tx.schedule.settings()).privacy.calendar_shows_patient_name:
            names["patient_name"] = (await tx.patients.display_names({entity.patient_id})).get(
                entity.patient_id
            )
        return view.model_copy(update=names)

    @staticmethod
    async def _require_notice(
        context: RequestContext, tx: Transaction, entity: Appointment, *, rescheduling: bool = False
    ) -> None:
        """A patient changing their own booking must respect the clinic's notice rules."""
        if context.actor_type != ActorType.CALLER:
            return
        rules = (await tx.schedule.settings()).scheduling
        notice = timedelta(minutes=rules.cancellation_notice_minutes)
        if (rescheduling and not rules.rescheduling_enabled) or (
            entity.starts_at - datetime.now(UTC) < notice
        ):
            raise TooLateToChangeError()

    @staticmethod
    async def _get(tx: Transaction, identity: UUID, *, lock: bool = False) -> Appointment:
        entity = await tx.appointments.get(identity, lock=lock)
        if entity is None:
            raise NotFoundError()
        return entity

    @staticmethod
    def _require_mutable(entity: Appointment, expected_version: int) -> None:
        if entity.version != expected_version:
            raise StaleAppointmentError()
        if entity.status != "booked" or entity.starts_at <= datetime.now(UTC):
            raise AppointmentStateError()
