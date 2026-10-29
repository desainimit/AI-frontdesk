"""An appointment time the way a receptionist says it on the phone.

"Mon 26 Oct, 5:30 PM" is fine in a text message; on a call people say "tomorrow at
five thirty in the evening", or in Hindi "कल शाम साढ़े पाँच बजे". Today and tomorrow
are named as such, the rest of this week by weekday, later dates with day and month.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.languages import CODES, ClockWords, normalize, pack

# Hinglish speakers say "Friday ko", not "Shukravaar ko": its pack names weekdays so.
_WEEKDAYS = {code: pack(code).weekdays for code in CODES}
_MONTHS = {code: pack(code).months for code in CODES}
_TODAY_TOMORROW = {code: pack(code).today_tomorrow for code in CODES}


def _day(local: datetime, today: date, code: str) -> str:
    days = (local.date() - today).days
    if days in (0, 1):
        return _TODAY_TOMORROW[code][days]
    weekday = _WEEKDAYS[code][local.weekday()]
    if 1 < days < 7:
        return {"en": "on " + weekday, "es": "el " + weekday, "fr": weekday}.get(code, weekday)
    month = _MONTHS[code][local.month - 1]
    return {
        "en": f"on {weekday}, {month} {local.day}",
        "es": f"el {weekday} {local.day} de {month}",
        "fr": f"le {weekday} {local.day} {month}",
        "de": f"am {weekday}, {local.day}. {month}",
        "pt": f"{weekday}, {local.day} de {month}",
    }.get(code, f"{weekday}, {local.day} {month}")


def _indian_clock(local: datetime, words: ClockWords) -> str:
    """Hindi, Hinglish and Gujarati: "saadhe paanch", "sava chaar", "paune chhah"."""
    hours = words.hours
    period = (
        words.morning
        if 4 <= local.hour < 12
        else words.afternoon
        if 12 <= local.hour < 16
        else words.evening
        if 16 <= local.hour < 20
        else words.night
    )
    hour, minute = local.hour % 12, local.minute
    if minute == 0:
        clock = f"{hours[hour]} {words.oclock}"
    elif minute == 30 and hour in (1, 2):
        # Half past one and two have their own words: "dedh", "dhaai".
        clock = f"{words.one_half if hour == 1 else words.two_half} {words.oclock}"
    elif minute == 30:
        clock = f"{words.half.format(hours[hour])} {words.oclock}"
    elif minute == 15:
        clock = f"{words.quarter} {hours[hour]} {words.oclock}"
    elif minute == 45:
        clock = f"{words.to} {hours[(hour + 1) % 12]} {words.oclock}"
    else:
        clock = words.minutes.format(hours[hour], minute)
    return f"{period} {clock}"


def _code(language: str) -> str:
    code = normalize(language)
    return code if code in _WEEKDAYS else "en"


def spoken_day(instant: datetime, timezone: str, language: str, now: datetime | None = None) -> str:
    """Just the day, as it fits mid-sentence: "on Friday", "शुक्रवार को", "કાલે"."""
    zone = ZoneInfo(timezone)
    local = instant.astimezone(zone)
    today = (now or datetime.now(zone)).astimezone(zone).date()
    code = _code(language)
    day = _day(local, today, code)
    if (local.date() - today).days < 2:
        return day
    if code == "hi":
        return f"{day} को"
    if code == "hi-Latn":
        return f"{day} ko"
    if code == "gu":
        return f"{_WEEKDAYS['gu'][local.weekday()]}ે"
    if code == "de" and not day.startswith("am "):
        return f"am {day}"
    if code == "pt":
        return f"para {day}"
    return day


def spoken_when(
    instant: datetime, timezone: str, language: str, now: datetime | None = None
) -> str:
    zone = ZoneInfo(timezone)
    local = instant.astimezone(zone)
    today = (now or datetime.now(zone)).astimezone(zone).date()
    code = _code(language)
    day = _day(local, today, code)
    clock_words = pack(code).clock
    if clock_words is not None:
        return f"{day} {_indian_clock(local, clock_words)}"
    if code == "en":
        hour = local.hour % 12 or 12
        clock = f"{hour}" if local.minute == 0 else f"{hour}:{local:%M}"
        return f"{day} at {clock} {'a.m.' if local.hour < 12 else 'p.m.'}"
    clock = f"{local:%H:%M}"
    return {
        "es": f"{day} a las {clock}",
        "fr": f"{day} à {local.hour} h {local:%M}" if local.minute else f"{day} à {local.hour} h",
        "de": f"{day} um {clock} Uhr",
        "pt": f"{day} às {clock}",
    }[code]
