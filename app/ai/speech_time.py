"""An appointment time the way a receptionist says it on the phone.

"Mon 26 Oct, 5:30 PM" is fine in a text message; on a call people say "tomorrow at
five thirty in the evening", or in Hindi "कल शाम साढ़े पाँच बजे". Today and tomorrow
are named as such, the rest of this week by weekday, later dates with day and month.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.conversation.locales import normalize_language

_HI_HOURS = ("बारह", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ", "दस", "ग्यारह")
_HINGLISH_HOURS = (
    "barah",
    "ek",
    "do",
    "teen",
    "chaar",
    "paanch",
    "chhah",
    "saat",
    "aath",
    "nau",
    "das",
    "gyarah",
)
_GU_HOURS = ("બાર", "એક", "બે", "ત્રણ", "ચાર", "પાંચ", "છ", "સાત", "આઠ", "નવ", "દસ", "અગિયાર")

_WEEKDAYS = {
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "hi": ("सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"),
    # Hinglish speakers say "Friday ko", not "Shukravaar ko".
    "hinglish": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "gu": ("સોમવાર", "મંગળવાર", "બુધવાર", "ગુરુવાર", "શુક્રવાર", "શનિવાર", "રવિવાર"),
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "fr": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
    "pt": ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"),
}
_MONTHS = {
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    "hi": (
        "जनवरी",
        "फ़रवरी",
        "मार्च",
        "अप्रैल",
        "मई",
        "जून",
        "जुलाई",
        "अगस्त",
        "सितंबर",
        "अक्टूबर",
        "नवंबर",
        "दिसंबर",
    ),
    "hinglish": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
    "gu": (
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
    ),
    "es": (
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ),
    "fr": (
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    ),
    "de": (
        "Januar",
        "Februar",
        "März",
        "April",
        "Mai",
        "Juni",
        "Juli",
        "August",
        "September",
        "Oktober",
        "November",
        "Dezember",
    ),
    "pt": (
        "janeiro",
        "fevereiro",
        "março",
        "abril",
        "maio",
        "junho",
        "julho",
        "agosto",
        "setembro",
        "outubro",
        "novembro",
        "dezembro",
    ),
}
_TODAY_TOMORROW = {
    "en": ("today", "tomorrow"),
    "hi": ("आज", "कल"),
    "hinglish": ("aaj", "kal"),
    "gu": ("આજે", "આવતીકાલે"),
    "es": ("hoy", "mañana"),
    "fr": ("aujourd'hui", "demain"),
    "de": ("heute", "morgen"),
    "pt": ("hoje", "amanhã"),
}


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


def _indian_clock(local: datetime, hours: tuple[str, ...], words: dict[str, str]) -> str:
    """Hindi, Hinglish and Gujarati: "saadhe paanch", "sava chaar", "paune chhah"."""
    period = (
        words["morning"]
        if 4 <= local.hour < 12
        else words["afternoon"]
        if 12 <= local.hour < 16
        else words["evening"]
        if 16 <= local.hour < 20
        else words["night"]
    )
    hour, minute = local.hour % 12, local.minute
    if minute == 0:
        clock = f"{hours[hour]} {words['oclock']}"
    elif minute == 30 and hour in (1, 2):
        # Half past one and two have their own words: "dedh", "dhaai".
        clock = f"{words['one_half' if hour == 1 else 'two_half']} {words['oclock']}"
    elif minute == 30:
        clock = f"{words['half'].format(hours[hour])} {words['oclock']}"
    elif minute == 15:
        clock = f"{words['quarter']} {hours[hour]} {words['oclock']}"
    elif minute == 45:
        clock = f"{words['to']} {hours[(hour + 1) % 12]} {words['oclock']}"
    else:
        clock = words["minutes"].format(hours[hour], minute)
    return f"{period} {clock}"


_HI_WORDS = {
    "morning": "सुबह",
    "afternoon": "दोपहर",
    "evening": "शाम",
    "night": "रात",
    "oclock": "बजे",
    "half": "साढ़े {}",
    "quarter": "सवा",
    "to": "पौने",
    "one_half": "डेढ़",
    "two_half": "ढाई",
    "minutes": "{} बजकर {} मिनट",
}
_HINGLISH_WORDS = {
    "morning": "subah",
    "afternoon": "dopahar",
    "evening": "shaam",
    "night": "raat",
    "oclock": "baje",
    "half": "saadhe {}",
    "quarter": "sava",
    "to": "paune",
    "one_half": "dedh",
    "two_half": "dhaai",
    "minutes": "{} bajkar {} minute",
}
_GU_WORDS = {
    "morning": "સવારે",
    "afternoon": "બપોરે",
    "evening": "સાંજે",
    "night": "રાત્રે",
    "oclock": "વાગ્યે",
    "half": "સાડા {}",
    "quarter": "સવા",
    "to": "પોણા",
    "one_half": "દોઢ",
    "two_half": "અઢી",
    "minutes": "{} વાગીને {} મિનિટે",
}


def spoken_day(instant: datetime, timezone: str, language: str, now: datetime | None = None) -> str:
    """Just the day, as it fits mid-sentence: "on Friday", "शुक्रवार को", "કાલે"."""
    zone = ZoneInfo(timezone)
    local = instant.astimezone(zone)
    today = (now or datetime.now(zone)).astimezone(zone).date()
    code = normalize_language(language)
    if code not in _WEEKDAYS:
        code = "en"
    day = _day(local, today, code)
    if (local.date() - today).days < 2:
        return day
    if code == "hi":
        return f"{day} को"
    if code == "hinglish":
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
    code = normalize_language(language)
    if code not in _WEEKDAYS:
        code = "en"
    day = _day(local, today, code)
    if code == "hi":
        return f"{day} {_indian_clock(local, _HI_HOURS, _HI_WORDS)}"
    if code == "hinglish":
        return f"{day} {_indian_clock(local, _HINGLISH_HOURS, _HINGLISH_WORDS)}"
    if code == "gu":
        return f"{day} {_indian_clock(local, _GU_HOURS, _GU_WORDS)}"
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
