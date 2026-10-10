from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.appointments.errors import SlotUnavailableError
from app.appointments.slots import ProviderCalendar, available_slots

# Monday 2026-10-12. Kolkata is UTC+05:30, so 09:00 local is 03:30 UTC.
MONDAY = date(2026, 10, 12)


def calendar(
    *, time_off: list[tuple[date, date]] | None = None, holidays: set[date] | None = None
) -> ProviderCalendar:
    return ProviderCalendar(
        timezone="Asia/Kolkata",
        windows=[(0, time(9), time(11)), (0, time(14), time(15)), (2, time(9), time(10))],
        time_off=time_off or [],
        holidays=frozenset(holidays or set()),
    )


def utc(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def test_slots_follow_the_weekly_schedule_and_the_service_length() -> None:
    week = {"starts_at": utc(MONDAY, 0), "ends_at": utc(MONDAY + timedelta(days=7), 0)}
    half_hours = available_slots(calendar(), [], minutes=30, **week)
    # Monday 09:00-11:00 and 14:00-15:00, Wednesday 09:00-10:00.
    assert len(half_hours) == 4 + 2 + 2
    assert half_hours[0].starts_at == utc(MONDAY, 3, 30)
    assert half_hours[0].ends_at == utc(MONDAY, 4, 0)
    # A 45-minute visit fits twice in the morning, once after lunch, once on Wednesday.
    assert len(available_slots(calendar(), [], minutes=45, **week)) == 2 + 1 + 1


def test_bookings_days_off_and_holidays_remove_slots() -> None:
    day = {"starts_at": utc(MONDAY, 0), "ends_at": utc(MONDAY + timedelta(days=1), 0)}
    busy = [(utc(MONDAY, 4, 0), utc(MONDAY, 4, 30))]  # 09:30-10:00 local
    starts = [slot.starts_at for slot in available_slots(calendar(), busy, minutes=30, **day)]
    assert utc(MONDAY, 4, 0) not in starts and utc(MONDAY, 3, 30) in starts and len(starts) == 5
    assert available_slots(calendar(time_off=[(MONDAY, MONDAY)]), [], minutes=30, **day) == []
    assert available_slots(calendar(holidays={MONDAY}), [], minutes=30, **day) == []
    # A range that starts mid-morning only offers what is still ahead.
    late = available_slots(
        calendar(), [], minutes=30, starts_at=utc(MONDAY, 4, 40), ends_at=utc(MONDAY, 12)
    )
    assert [slot.starts_at for slot in late] == [
        utc(MONDAY, 5, 0),
        utc(MONDAY, 8, 30),
        utc(MONDAY, 9, 0),
    ]


def test_a_booking_must_sit_inside_working_hours() -> None:
    assert calendar().require_bookable(utc(MONDAY, 3, 30), 30) == utc(MONDAY, 4, 0)
    assert calendar().require_bookable(utc(MONDAY, 3, 45), 30) == utc(MONDAY, 4, 15)
    for starts_at, minutes in (
        (utc(MONDAY, 3, 0), 30),  # before opening
        (utc(MONDAY, 5, 15), 30),  # runs past the end of the morning
        (utc(MONDAY, 3, 31), 30),  # not on a five-minute mark
        (utc(MONDAY + timedelta(days=1), 3, 30), 30),  # Tuesday: not a working day
    ):
        with pytest.raises(SlotUnavailableError):
            calendar().require_bookable(starts_at, minutes)
    with pytest.raises(SlotUnavailableError):
        calendar(holidays={MONDAY}).require_bookable(utc(MONDAY, 3, 30), 30)
