"""The emergency and crisis numbers a caller is told, for the country the clinic is in.

Found from the clinic's own phone number (its country code), or else from its time
zone. A country not listed gets "your local emergency number" and no crisis line.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmergencyLines:
    # Spaced, so they are read digit by digit: "one one two", not "a hundred and twelve".
    emergency: str = ""
    crisis: str = ""


# Calling code -> (emergency, crisis line).
_BY_CODE: dict[str, EmergencyLines] = {
    # India: national emergency number; Tele-MANAS, the national mental health line.
    "91": EmergencyLines("1 1 2", "1 4 4 1 6"),
    # United States and Canada: 911; the 988 Suicide and Crisis Lifeline.
    "1": EmergencyLines("9 1 1", "9 8 8"),
    # United Kingdom: 999; Samaritans.
    "44": EmergencyLines("9 9 9", "1 1 6, 1 2 3"),
    # Australia: Triple Zero; Lifeline.
    "61": EmergencyLines("0 0 0", "13, 11, 14"),
    # The European emergency number.
    **{code: EmergencyLines("1 1 2") for code in ("33", "34", "39", "49", "31", "351", "353")},
}
_BY_ZONE: dict[str, str] = {
    "Asia/Kolkata": "91",
    "Asia/Calcutta": "91",
    "Europe/London": "44",
}
_ZONE_PREFIX: dict[str, str] = {"America/": "1", "Australia/": "61"}


def emergency_lines(phone: str, timezone: str) -> EmergencyLines:
    digits = phone.lstrip("+") if phone.startswith("+") else ""
    for size in (3, 2, 1):
        if digits[:size] in _BY_CODE:
            return _BY_CODE[digits[:size]]
    code = _BY_ZONE.get(timezone) or next(
        (code for prefix, code in _ZONE_PREFIX.items() if timezone.startswith(prefix)), None
    )
    return _BY_CODE.get(code or "", EmergencyLines())
