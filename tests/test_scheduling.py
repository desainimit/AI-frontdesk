from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.appointments.commands import BookAppointment, DefineAvailability, SlotQuery
from app.appointments.errors import SlotUnavailableError
from app.appointments.models import Appointment, Availability
from app.appointments.slots import available_slots, slot_end
from app.locations.schemas import CreateLocation
from app.patients.schemas import CreatePatient

START = datetime(2030, 1, 1, 9, tzinfo=UTC)


def window() -> Availability:
    return Availability(starts_at=START, ends_at=START + timedelta(hours=2), slot_minutes=30)


@pytest.mark.parametrize("minutes", [-30, 1, 119, 120])
def test_booking_must_fit_window_and_slot_grid(minutes: int) -> None:
    with pytest.raises(SlotUnavailableError):
        slot_end(window(), START + timedelta(minutes=minutes))


def test_adjacent_slots_busy_ranges_and_partial_query() -> None:
    busy = Appointment(starts_at=START + timedelta(minutes=30), ends_at=START + timedelta(hours=1))
    query = SlotQuery(starts_at=START, ends_at=START + timedelta(hours=2))
    slots = available_slots([window()], [busy], query, START - timedelta(days=1))
    assert [s.starts_at for s in slots] == [
        START,
        START + timedelta(hours=1),
        START + timedelta(minutes=90),
    ]
    query = SlotQuery(
        starts_at=START + timedelta(minutes=1), ends_at=START + timedelta(minutes=119)
    )
    assert [s.starts_at for s in available_slots([window()], [busy], query, START)] == [
        START + timedelta(hours=1)
    ]
    assert slot_end(window(), START + timedelta(minutes=90)) == START + timedelta(hours=2)


def test_slots_exclude_present_and_past() -> None:
    query = SlotQuery(starts_at=START, ends_at=START + timedelta(hours=2))
    assert [
        s.starts_at for s in available_slots([window()], [], query, START + timedelta(hours=1))
    ] == [START + timedelta(minutes=90)]


def test_timezones_normalize_offsets_and_distinguish_dst_fold() -> None:
    first = BookAppointment.model_validate(
        {"provider_id": uuid4(), "patient_id": uuid4(), "starts_at": "2030-11-03T01:30:00-04:00"}
    )
    second = first.model_copy(
        update={"starts_at": datetime.fromisoformat("2030-11-03T01:30:00-05:00").astimezone(UTC)}
    )
    assert second.starts_at - first.starts_at == timedelta(hours=1)
    assert first.starts_at.tzinfo == UTC
    with pytest.raises(ValidationError):
        BookAppointment(
            provider_id=uuid4(), patient_id=uuid4(), starts_at=START.replace(tzinfo=None)
        )


@pytest.mark.parametrize("hours,minutes", [(25, 30), (1, 120), (0, 30), (1, 4), (5, 241)])
def test_availability_bounds(hours: int, minutes: int) -> None:
    with pytest.raises(ValidationError):
        DefineAvailability(
            starts_at=START, ends_at=START + timedelta(hours=hours), slot_minutes=minutes
        )


def test_query_bounds_and_untrusted_fields() -> None:
    with pytest.raises(ValidationError):
        SlotQuery(starts_at=START, ends_at=START + timedelta(days=32))
    with pytest.raises(ValidationError):
        CreatePatient.model_validate(
            {"display_name": "Test", "phone": "+919999999999", "tenant_id": str(uuid4())}
        )
    with pytest.raises(ValidationError):
        CreatePatient(display_name="  ", phone="999")
    with pytest.raises(ValidationError):
        CreateLocation(name="Test", timezone="invalid/timezone")
    assert CreateLocation(name=" Test ", timezone="Asia/Kolkata").name == "Test"
