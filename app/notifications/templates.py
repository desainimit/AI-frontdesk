"""Turning a clinic's wording into the message one patient receives."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from app.conversation.locales import base_language

_VARIABLE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
# Twilio splits longer texts into several billed parts; keep patient messages short.
SMS_LIMIT = 480


def render(text: str, values: dict[str, str]) -> str:
    """Fill `{{name}}` placeholders. An unknown placeholder is left out, not shown raw."""
    return _VARIABLE.sub(lambda match: values.get(match.group(1), ""), text).strip()


def when(instant: datetime, timezone: str, language: str = "en") -> str:
    """An appointment time as the patient reads it: `Mon 12 Oct, 10:30 AM`."""
    local = instant.astimezone(ZoneInfo(timezone))
    hour = local.hour % 12 or 12
    if base_language(language) == "gu":
        weekdays = ("સોમવાર", "મંગળવાર", "બુધવાર", "ગુરુવાર", "શુક્રવાર", "શનિવાર", "રવિવાર")
        months = (
            "જાન્યુઆરી",
            "ફેબ્રુઆરી",
            "માર્ચ",
            "એપ્રિલ",
            "મે",
            "જૂન",
            "જુલાઈ",
            "ઑગસ્ટ",
            "સપ્ટેમ્બર",
            "ઑક્ટોબર",
            "નવેમ્બર",
            "ડિસેમ્બર",
        )
        period = (
            "સવારે"
            if 5 <= local.hour < 12
            else "બપોરે"
            if 12 <= local.hour < 17
            else "સાંજે"
            if 17 <= local.hour < 20
            else "રાત્રે"
        )
        return (
            f"{weekdays[local.weekday()]}, {local.day} {months[local.month - 1]} "
            f"{local.year}, {period} {hour}:{local:%M}"
        )
    return (
        f"{local:%a} {local.day} {local:%b}, {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'}"
    )


def mask(recipient: str) -> str:
    """A recipient as shown in a delivery log: enough to recognise, not enough to copy."""
    if "@" in recipient:
        name, _, domain = recipient.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"{'*' * max(len(recipient) - 4, 0)}{recipient[-4:]}"
