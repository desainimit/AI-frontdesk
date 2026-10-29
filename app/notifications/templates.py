"""Turning a clinic's wording into the message one patient receives."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.languages import pack

_VARIABLE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
# Twilio splits longer texts into several billed parts; keep patient messages short.
SMS_LIMIT = 480


def render(text: str, values: dict[str, str]) -> str:
    """Fill `{{name}}` placeholders. An unknown placeholder is left out, not shown raw."""
    return _VARIABLE.sub(lambda match: values.get(match.group(1), ""), text).strip()


def when(instant: datetime, timezone: str, language: str = "en") -> str:
    """An appointment time as the patient reads it, in their language: `Mon 12 Oct,
    10:30 AM`, `lunes 12 de octubre, 10:30`, `सोमवार, 12 अक्टूबर, सुबह 10:30`."""
    local = instant.astimezone(ZoneInfo(timezone))
    words = pack(language)
    hour = local.hour % 12 or 12
    clock = words.clock
    period = ""
    if clock is not None:
        period = (
            clock.morning
            if 4 <= local.hour < 12
            else clock.afternoon
            if 12 <= local.hour < 16
            else clock.evening
            if 16 <= local.hour < 20
            else clock.night
        )
    weekday, month = words.weekdays[local.weekday()], words.months[local.month - 1]
    return words.clinic_texts["date"].format(
        weekday=weekday,
        weekday3=weekday[:3],
        month=month,
        month3=month[:3],
        day=local.day,
        year=local.year,
        time12=f"{hour}:{local:%M}",
        time24=f"{local:%H:%M}",
        ampm="AM" if local.hour < 12 else "PM",
        period=period,
    )


def mask(recipient: str) -> str:
    """A recipient as shown in a delivery log: enough to recognise, not enough to copy."""
    if "@" in recipient:
        name, _, domain = recipient.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"{'*' * max(len(recipient) - 4, 0)}{recipient[-4:]}"
