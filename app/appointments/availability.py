from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.appointments.commands import SlotQuery
from app.appointments.schemas import Slot
from app.appointments.slots import available_slots
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.unit_of_work import Transaction, UnitOfWork
from app.core.errors import NotFoundError
from app.settings.schemas import ClinicConfiguration


async def appointment_minutes(
    tx: Transaction, settings: ClinicConfiguration, service_id: UUID | None
) -> int:
    """How long an appointment of this type lasts; the clinic's usual length without one."""
    if service_id is None:
        return settings.scheduling.default_appointment_minutes
    service = await tx.schedule.service(service_id)
    if service is None or not service.is_active:
        raise NotFoundError()
    return service.duration_minutes


def booking_window(
    context: RequestContext, settings: ClinicConfiguration, now: datetime
) -> tuple[datetime, datetime | None]:
    """Earliest and latest start a booking may have.

    The clinic's notice and how-far-ahead rules are for patients booking through the
    receptionist. Staff may book any future time.
    """
    if context.actor_type != ActorType.CALLER:
        return now, None
    rules = settings.scheduling
    return (
        now + timedelta(minutes=rules.minimum_notice_minutes),
        now + timedelta(days=rules.booking_horizon_days),
    )


class AvailabilityService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def slots(
        self,
        context: RequestContext,
        provider_id: UUID,
        query: SlotQuery,
        service_id: UUID | None = None,
    ) -> list[Slot]:
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._uow.transaction(context) as tx:
            settings = await tx.schedule.settings()
            calendar = await tx.schedule.calendar(provider_id, settings)
            if calendar is None:
                raise NotFoundError()
            minutes = await appointment_minutes(tx, settings, service_id)
            earliest, latest = booking_window(context, settings, datetime.now(UTC))
            starts_at = max(query.starts_at, earliest)
            ends_at = min(query.ends_at, latest) if latest else query.ends_at
            if ends_at <= starts_at:
                return []
            occupied = await tx.appointments.between(provider_id, starts_at, ends_at)
            return available_slots(
                calendar,
                [(row.starts_at, row.ends_at) for row in occupied],
                starts_at=starts_at,
                ends_at=ends_at,
                minutes=minutes,
            )
