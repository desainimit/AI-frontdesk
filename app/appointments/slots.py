from collections.abc import Sequence
from datetime import datetime, timedelta

from app.appointments.commands import SlotQuery
from app.appointments.errors import SlotUnavailableError
from app.appointments.models import Appointment, Availability
from app.appointments.schemas import Slot


def slot_end(window: Availability | None, starts_at: datetime) -> datetime:
    if window is None:
        raise SlotUnavailableError()
    step = timedelta(minutes=window.slot_minutes)
    end = starts_at + step
    if (
        starts_at < window.starts_at
        or (starts_at - window.starts_at) % step
        or end > window.ends_at
    ):
        raise SlotUnavailableError()
    return end


def available_slots(
    windows: Sequence[Availability],
    occupied: Sequence[Appointment],
    query: SlotQuery,
    now: datetime,
) -> list[Slot]:
    """Half-open UTC intervals; sorted non-overlapping windows permit a linear busy scan."""
    result: list[Slot] = []
    busy_index = 0
    for window in windows:
        step = timedelta(minutes=window.slot_minutes)
        lower = max(query.starts_at, now)
        candidate = window.starts_at + max(0, (lower - window.starts_at) // step) * step
        if candidate < lower or candidate <= now:
            candidate += step
        upper = min(query.ends_at, window.ends_at)
        while candidate + step <= upper:
            end = candidate + step
            while busy_index < len(occupied) and occupied[busy_index].ends_at <= candidate:
                busy_index += 1
            if busy_index == len(occupied) or occupied[busy_index].starts_at >= end:
                result.append(Slot(starts_at=candidate, ends_at=end))
            candidate = end
    return result
