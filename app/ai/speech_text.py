"""Text rewritten the way a person would read it aloud, just before it is spoken.

Speech models read "10:30" as "ten colon thirty" or "one thousand thirty", and a phone
number as one huge number. The transcript keeps the written form; only the audio uses
this one.
"""

import re

from app.conversation.locales import normalize_language

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}

_TIME = re.compile(
    r"\b(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?:\s*(?P<half>[ap])\.?\s?m\.?)?(?![\w:])",
    re.IGNORECASE,
)
# A run of at least six digits, possibly grouped with spaces or dashes: a phone number,
# a code or a reference. Read digit by digit, a short pause between groups.
_DIGITS = re.compile(r"\+?\d[\d -]{4,}\d")
_TITLES = re.compile(r"\b(Dr|Mr|Mrs|Ms)\.\s")
_TITLE_WORDS = {"Dr": "Doctor", "Mr": "Mister", "Mrs": "Missus", "Ms": "Miz"}


def _number(value: int) -> str:
    if value < 20:
        return _ONES[value]
    tens, ones = divmod(value, 10)
    return _TENS[tens] + ("" if ones == 0 else " " + _ONES[ones])


def _time(match: re.Match[str]) -> str:
    hour, minute = int(match["hour"]), int(match["minute"])
    half = (match["half"] or "").lower()
    if half and hour > 12:
        return match.group()
    if not half and hour > 12:
        # A 24-hour time is said on the 12-hour clock: "14:30" is "two thirty p.m.".
        hour, half = hour - 12, "p"
    spoken_hour = _number(hour) if hour else "twelve"
    if minute == 0:
        words = spoken_hour + ("" if half else " o'clock")
    elif minute < 10:
        words = f"{spoken_hour} oh {_ONES[minute]}"
    else:
        words = f"{spoken_hour} {_number(minute)}"
    return words + (f" {half}.m." if half else "")


def _digits(match: re.Match[str]) -> str:
    text = match.group()
    if sum(c.isdigit() for c in text) < 6 or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    groups = [group for group in re.split(r"[ -]+", text) if group]
    spoken = [
        ("plus " if group.startswith("+") else "") + " ".join(c for c in group if c.isdigit())
        for group in groups
    ]
    return ", ".join(spoken)


def speakable(text: str, language: str | None = None) -> str:
    code = normalize_language(language or "en")
    text = _DIGITS.sub(_digits, text)
    if code in {"en", "hinglish"}:
        text = _TIME.sub(_time, text).replace(".m..", ".m.")
        text = _TITLES.sub(lambda match: _TITLE_WORDS[match[1]] + " ", text)
        text = text.replace(" & ", " and ")
    return text
