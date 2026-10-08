from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.appointments.schemas import AppointmentView
from app.events.outbox import EventType
from app.events.providers import Notification, PermanentProviderError, TransientProviderError
from app.integrations.appointment import AppointmentOutboxAdapter, TenantOutboxRouter
from app.integrations.ports import CalendarChange, CustomerAppointmentChange


class CalendarSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[CalendarChange, str]] = []

    async def apply_appointment(self, change: CalendarChange, *, idempotency_key: str) -> None:
        self.calls.append((change, idempotency_key))


class CustomerSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[CustomerAppointmentChange, str]] = []

    async def apply_appointment(
        self, change: CustomerAppointmentChange, *, idempotency_key: str
    ) -> None:
        self.calls.append((change, idempotency_key))


class FlakyEHR(CustomerSpy):
    async def apply_appointment(
        self, change: CustomerAppointmentChange, *, idempotency_key: str
    ) -> None:
        await super().apply_appointment(change, idempotency_key=idempotency_key)
        if len(self.calls) == 1:
            raise TransientProviderError()


def notification(
    event_type: EventType = EventType.APPOINTMENT_CREATED,
) -> Notification:
    now = datetime.now(UTC)
    appointment = AppointmentView(
        id=uuid4(),
        tenant_id=uuid4(),
        provider_id=uuid4(),
        patient_id=uuid4(),
        starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=1, minutes=30),
        status="cancelled" if event_type == EventType.APPOINTMENT_CANCELLED else "booked",
        version=1,
        created_at=now,
        updated_at=now,
    )
    return Notification(uuid4(), appointment.tenant_id, event_type, appointment)


async def test_outbox_adapter_projects_only_required_fields() -> None:
    calendar, crm, ehr = CalendarSpy(), CustomerSpy(), CustomerSpy()
    event = notification(EventType.APPOINTMENT_RESCHEDULED)
    adapter = AppointmentOutboxAdapter(event.tenant_id, calendar=calendar, crm=crm, ehr=ehr)
    await adapter.send(event, idempotency_key=str(event.event_id))
    assert [len(provider.calls) for provider in (calendar, crm, ehr)] == [1, 1, 1]
    calendar_change, key = calendar.calls[0]
    assert key == str(event.event_id)
    assert calendar_change.external_uid == (f"frontdesk-{event.tenant_id}-{event.appointment.id}")
    assert "patient_id" not in asdict(calendar_change)
    assert crm.calls[0][0].patient_id == ehr.calls[0][0].patient_id == event.appointment.patient_id
    assert crm.calls[0][1] == ehr.calls[0][1] == key


@pytest.mark.parametrize(
    "changed",
    ["tenant", "status", "time", "key"],
)
async def test_invalid_outbox_events_never_reach_a_provider(changed: str) -> None:
    event = notification()
    calendar = CalendarSpy()
    adapter = AppointmentOutboxAdapter(event.tenant_id, calendar=calendar)
    key = str(event.event_id)
    if changed == "tenant":
        event = Notification(event.event_id, uuid4(), event.event_type, event.appointment)
    elif changed == "status":
        event = Notification(
            event.event_id,
            event.tenant_id,
            event.event_type,
            event.appointment.model_copy(update={"status": "cancelled"}),
        )
    elif changed == "time":
        event = Notification(
            event.event_id,
            event.tenant_id,
            event.event_type,
            event.appointment.model_copy(update={"ends_at": event.appointment.starts_at}),
        )
    else:
        key = str(uuid4())
    with pytest.raises(PermanentProviderError):
        await adapter.send(event, idempotency_key=key)
    assert calendar.calls == []


async def test_partial_failure_reuses_destination_keys_on_retry() -> None:
    calendar, ehr = CalendarSpy(), FlakyEHR()
    event = notification()
    adapter = AppointmentOutboxAdapter(event.tenant_id, calendar=calendar, ehr=ehr)
    key = str(event.event_id)
    with pytest.raises(TransientProviderError):
        await adapter.send(event, idempotency_key=key)
    await adapter.send(event, idempotency_key=key)
    assert [attempt[1] for attempt in calendar.calls] == [key, key]
    assert [attempt[1] for attempt in ehr.calls] == [key, key]


def test_outbox_adapter_requires_a_destination() -> None:
    with pytest.raises(ValueError):
        AppointmentOutboxAdapter(uuid4())


async def test_tenant_router_blocks_unconfigured_and_cross_tenant_delivery() -> None:
    first, second = notification(), notification()
    first_calendar, second_calendar = CalendarSpy(), CalendarSpy()
    router = TenantOutboxRouter(
        {
            first.tenant_id: AppointmentOutboxAdapter(first.tenant_id, calendar=first_calendar),
            second.tenant_id: AppointmentOutboxAdapter(second.tenant_id, calendar=second_calendar),
        }
    )
    await router.send(first, idempotency_key=str(first.event_id))
    assert len(first_calendar.calls) == 1 and second_calendar.calls == []
    unknown = notification()
    with pytest.raises(PermanentProviderError):
        await router.send(unknown, idempotency_key=str(unknown.event_id))
    assert second_calendar.calls == []
    with pytest.raises(PermanentProviderError):
        await AppointmentOutboxAdapter(second.tenant_id, calendar=second_calendar).send(
            first, idempotency_key=str(first.event_id)
        )
    assert second_calendar.calls == []
