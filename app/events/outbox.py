from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.schemas import AppointmentView
from app.auth.context import RequestContext
from app.auth.policies import require_tenant
from app.events.models import OutboxEvent


class EventType(StrEnum):
    APPOINTMENT_CREATED = "appointment.created"
    APPOINTMENT_CANCELLED = "appointment.cancelled"
    APPOINTMENT_RESCHEDULED = "appointment.rescheduled"


class OutboxRepository:
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session, self._context = session, context

    def appointment(self, event_type: EventType, appointment: AppointmentView) -> None:
        require_tenant(self._context, appointment.tenant_id)
        self._session.add(
            OutboxEvent(
                tenant_id=self._context.tenant_id,
                aggregate_id=appointment.id,
                aggregate_version=appointment.version,
                event_type=event_type,
                schema_version=1,
                request_id=self._context.request_id,
                payload=appointment.model_dump(mode="json"),
            )
        )
