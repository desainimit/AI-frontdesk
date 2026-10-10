"""Bookable time = weekly schedule, minus days off and clinic holidays, minus bookings."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.appointments.errors import SlotUnavailableError
from app.appointments.schemas import Slot

# Appointments start on a five-minute mark, whatever their length.
GRID = timedelta(minutes=5)


@dataclass(frozen=True)
class ProviderCalendar:
    """What decides when one provider can be booked."""

    timezone: str
    # (weekday, starts, ends) in the location's clock; weekday 0 is Monday.
    windows: Sequence[tuple[int, time, time]]
    # Inclusive (first day, last day) pairs.
    time_off: Sequence[tuple[date, date]]
    holidays: frozenset[date]

    def working(self, day: date) -> list[tuple[datetime, datetime]]:
        """The provider's working windows on a local day, as UTC instants."""
        if day in self.holidays or any(first <= day <= last for first, last in self.time_off):
            return []
        zone = ZoneInfo(self.timezone)
        return sorted(
            (
                datetime.combine(day, starts, zone).astimezone(UTC),
                datetime.combine(day, ends, zone).astimezone(UTC),
            )
            for weekday, starts, ends in self.windows
            if weekday == day.weekday()
        )

    def require_bookable(self, starts_at: datetime, minutes: int) -> datetime:
        """The end of an appointment starting here, or refuse it."""
        ends_at = starts_at + timedelta(minutes=minutes)
        offset = starts_at - datetime(1970, 1, 1, tzinfo=UTC)
        local_day = starts_at.astimezone(ZoneInfo(self.timezone)).date()
        if offset % GRID or not any(
            opens <= starts_at and ends_at <= closes for opens, closes in self.working(local_day)
        ):
            raise SlotUnavailableError()
        return ends_at


def available_slots(
    calendar: ProviderCalendar,
    busy: Sequence[tuple[datetime, datetime]],
    *,
    starts_at: datetime,
    ends_at: datetime,
    minutes: int,
) -> list[Slot]:
    """Free slots of one length inside [starts_at, ends_at). ``busy`` is sorted by start."""
    step = timedelta(minutes=minutes)
    zone = ZoneInfo(calendar.timezone)
    day = starts_at.astimezone(zone).date()
    last_day = ends_at.astimezone(zone).date()
    result: list[Slot] = []
    index = 0
    while day <= last_day:
        for opens, closes in calendar.working(day):
            candidate = opens
            if candidate < starts_at:
                # First slot of this window at or after the requested start.
                candidate += -((opens - starts_at) // step) * step
            while candidate + step <= min(closes, ends_at):
                end = candidate + step
                while index < len(busy) and busy[index][1] <= candidate:
                    index += 1
                if index == len(busy) or busy[index][0] >= end:
                    result.append(Slot(starts_at=candidate, ends_at=end))
                candidate = end
        day += timedelta(days=1)
    return result
