"""Turning a clinic's wording into the message one patient receives."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

_VARIABLE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
# Twilio splits longer texts into several billed parts; keep patient messages short.
SMS_LIMIT = 480


def render(text: str, values: dict[str, str]) -> str:
    """Fill `{{name}}` placeholders. An unknown placeholder is left out, not shown raw."""
    return _VARIABLE.sub(lambda match: values.get(match.group(1), ""), text).strip()


def when(instant: datetime, timezone: str) -> str:
    """An appointment time as the patient reads it: `Mon 12 Oct, 10:30 AM`."""
    local = instant.astimezone(ZoneInfo(timezone))
    hour = local.hour % 12 or 12
    return (
        f"{local:%a} {local.day} {local:%b}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"
    )


def mask(recipient: str) -> str:
    """A recipient as shown in a delivery log: enough to recognise, not enough to copy."""
    if "@" in recipient:
        name, _, domain = recipient.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"{'*' * max(len(recipient) - 4, 0)}{recipient[-4:]}"
