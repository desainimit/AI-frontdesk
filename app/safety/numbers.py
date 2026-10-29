"""The country a clinic is in, and the emergency and crisis numbers a caller is told.

The country is found from the clinic's own phone number (its calling code), or else
from its time zone. It also picks the accent of the voice (English in India sounds
Indian). A country not listed gets "your local emergency number" and no crisis line.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmergencyLines:
    # Spaced, so they are read digit by digit: "one one two", not "a hundred and twelve".
    emergency: str = ""
    crisis: str = ""


# Calling code -> ISO country.
_COUNTRIES: dict[str, str] = {
    "91": "IN",
    "1": "US",
    "44": "GB",
    "61": "AU",
    "34": "ES",
    "33": "FR",
    "49": "DE",
    "43": "AT",
    "41": "CH",
    "351": "PT",
    "55": "BR",
    "52": "MX",
    "39": "IT",
    "31": "NL",
    "353": "IE",
}
# Country -> (emergency, crisis line). Crisis lines are free national lines open day
# and night.
_LINES: dict[str, EmergencyLines] = {
    # India: national emergency number; Tele-MANAS, the national mental health line.
    "IN": EmergencyLines("1 1 2", "1 4 4 1 6"),
    # United States and Canada: 911; the 988 Suicide and Crisis Lifeline.
    "US": EmergencyLines("9 1 1", "9 8 8"),
    # United Kingdom and Ireland: Samaritans.
    "GB": EmergencyLines("9 9 9", "1 1 6, 1 2 3"),
    "IE": EmergencyLines("1 1 2", "1 1 6, 1 2 3"),
    # Australia: Triple Zero; Lifeline.
    "AU": EmergencyLines("0 0 0", "13, 11, 14"),
    # Spain: Línea 024; France: 3114; Germany: TelefonSeelsorge; Portugal: SNS 24.
    "ES": EmergencyLines("1 1 2", "0 2 4"),
    "FR": EmergencyLines("1 1 2", "3 1 1 4"),
    "DE": EmergencyLines("1 1 2", "0 8 0 0, 1 1 1 0, 1 1 1"),
    "PT": EmergencyLines("1 1 2", "8 0 8, 2 4, 2 4, 2 4"),
    # Brazil: SAMU 192; CVV 188. Mexico: 911; Línea de la Vida.
    "BR": EmergencyLines("1 9 2", "1 8 8"),
    "MX": EmergencyLines("9 1 1", "8 0 0, 9 1 1, 2 0 0 0"),
    # The European emergency number, and the Netherlands' 113 line.
    "NL": EmergencyLines("1 1 2", "1 1 3"),
    "IT": EmergencyLines("1 1 2"),
    "AT": EmergencyLines("1 1 2"),
    "CH": EmergencyLines("1 1 2"),
}
_ZONES: dict[str, str] = {
    "Asia/Kolkata": "IN",
    "Asia/Calcutta": "IN",
    "Europe/London": "GB",
    "Europe/Dublin": "IE",
    "Europe/Madrid": "ES",
    "Europe/Paris": "FR",
    "Europe/Berlin": "DE",
    "Europe/Vienna": "AT",
    "Europe/Zurich": "CH",
    "Europe/Lisbon": "PT",
    "Europe/Rome": "IT",
    "Europe/Amsterdam": "NL",
    "America/Sao_Paulo": "BR",
    "America/Mexico_City": "MX",
}
# Checked after the exact zones above: most of the Americas' zones are not the US.
_ZONE_PREFIX: dict[str, str] = {
    "America/New_York": "US",
    "America/Chicago": "US",
    "America/Denver": "US",
    "America/Los_Angeles": "US",
    "America/Phoenix": "US",
    "America/Anchorage": "US",
    "America/Toronto": "US",
    "America/Vancouver": "US",
    "Australia/": "AU",
}


def clinic_country(phone: str, timezone: str) -> str | None:
    """The clinic's country (ISO 3166 alpha-2), or None when it cannot be told."""
    digits = phone.lstrip("+") if phone.startswith("+") else ""
    for size in (3, 2, 1):
        if digits[:size] in _COUNTRIES:
            return _COUNTRIES[digits[:size]]
    if timezone in _ZONES:
        return _ZONES[timezone]
    return next(
        (country for prefix, country in _ZONE_PREFIX.items() if timezone.startswith(prefix)),
        None,
    )


def emergency_lines(phone: str, timezone: str) -> EmergencyLines:
    return _LINES.get(clinic_country(phone, timezone) or "", EmergencyLines())
