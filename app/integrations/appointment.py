"""Adapt committed appointment outbox events to external integration ports."""

from collections.abc import Mapping
from datetime import UTC
from uuid import UUID

from app.events.outbox import EventType
from app.events.providers import Notification, PermanentProviderError
from app.integrations.ports import (
    CalendarChange,
    CalendarProvider,
    CRMProvider,
    CustomerAppointmentChange,
    EHRProvider,
)


class AppointmentOutboxAdapter:
    """One worker delivery can fan out; each destination must deduplicate retries."""

    def __init__(
        self,
        tenant_id: UUID,
        *,
        calendar: CalendarProvider | None = None,
        crm: CRMProvider | None = None,
        ehr: EHRProvider | None = None,
    ) -> None:
        if calendar is None and crm is None and ehr is None:
            raise ValueError("Configure at least one appointment integration")
        self.tenant_id = tenant_id
        self.calendar, self.crm, self.ehr = calendar, crm, ehr

    async def send(self, notification: Notification, *, idempotency_key: str) -> None:
        appointment = notification.appointment
        if (
            idempotency_key != str(notification.event_id)
            or notification.tenant_id != self.tenant_id
            or appointment.tenant_id != notification.tenant_id
            or appointment.version < 1
            or appointment.starts_at.tzinfo is None
            or appointment.ends_at.tzinfo is None
            or appointment.starts_at.utcoffset() is None
            or appointment.ends_at.utcoffset() is None
            or appointment.ends_at <= appointment.starts_at
            or (
                notification.event_type == EventType.APPOINTMENT_CANCELLED
                and appointment.status != "cancelled"
            )
            or (
                notification.event_type != EventType.APPOINTMENT_CANCELLED
                and appointment.status != "booked"
            )
        ):
            raise PermanentProviderError()
        # All destinations receive the same event UUID on every retry. A calendar
        # receives no patient identifier; CRM/EHR receive only the IDs they need.
        if self.calendar is not None:
            await self.calendar.apply_appointment(
                CalendarChange(
                    event_id=notification.event_id,
                    tenant_id=notification.tenant_id,
                    appointment_id=appointment.id,
                    external_uid=f"frontdesk-{notification.tenant_id}-{appointment.id}",
                    provider_id=appointment.provider_id,
                    event_type=notification.event_type,
                    version=appointment.version,
                    starts_at=appointment.starts_at.astimezone(UTC),
                    ends_at=appointment.ends_at.astimezone(UTC),
                ),
                idempotency_key=idempotency_key,
            )
        if self.crm is not None or self.ehr is not None:
            change = CustomerAppointmentChange(
                event_id=notification.event_id,
                tenant_id=notification.tenant_id,
                appointment_id=appointment.id,
                patient_id=appointment.patient_id,
                provider_id=appointment.provider_id,
                event_type=notification.event_type,
                version=appointment.version,
                starts_at=appointment.starts_at.astimezone(UTC),
                ends_at=appointment.ends_at.astimezone(UTC),
            )
            if self.crm is not None:
                await self.crm.apply_appointment(change, idempotency_key=idempotency_key)
            if self.ehr is not None:
                await self.ehr.apply_appointment(change, idempotency_key=idempotency_key)


class TenantOutboxRouter:
    """Require an explicit provider set for each tenant handled by a worker."""

    def __init__(self, adapters: Mapping[UUID, AppointmentOutboxAdapter]) -> None:
        if not adapters or any(key != adapter.tenant_id for key, adapter in adapters.items()):
            raise ValueError("Tenant integration routing must be explicit")
        self._adapters = dict(adapters)

    async def send(self, notification: Notification, *, idempotency_key: str) -> None:
        adapter = self._adapters.get(notification.tenant_id)
        if adapter is None:
            raise PermanentProviderError()
        await adapter.send(notification, idempotency_key=idempotency_key)
