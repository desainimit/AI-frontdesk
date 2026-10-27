"""The receptionist's check before a caller's appointments are shown or changed.

A caller ID can be faked, so on a call a matching number alone does not open someone's
appointments (unless the clinic trusts numbers). Instead, as a person at the desk would:
- calling from the patient's own number, they say the name on the appointment;
- calling from any other number, they give the appointment number (A-3) and the name.
Every move or cancellation also sends a message to the patient's own phone.
"""

import re
from difflib import SequenceMatcher

from app.ai.romanize import latin_fold, sound_fold

# Not part of a name when said aloud.
_TITLES = frozenset(
    {"mr", "mrs", "ms", "miss", "dr", "doctor", "shri", "smt", "ji", "sir", "madam"}
)
_SPOKEN_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}  # fmt: skip
# Close enough to be the same name misheard ("Nimith" for "Nimit").
SIMILAR = 0.8


def _words(name: str) -> list[str]:
    """The parts of a name in Latin letters, whatever script it was said or stored in."""
    words: list[str] = []
    # Spelled out ("N I M I T. D E S A I"): the letters of one part make one word.
    for part in re.split(r"[.,;]", latin_fold(name)):
        letters = ""
        for word in re.findall(r"[^\W\d_]+", part):
            if len(word) == 1:
                letters += word
                continue
            if letters:
                words.append(letters)
                letters = ""
            words.append(word)
        if letters:
            words.append(letters)
    return [word for word in words if word not in _TITLES]


def _close(a: str, b: str) -> bool:
    return (
        a == b
        or SequenceMatcher(None, a, b).ratio() >= SIMILAR
        or SequenceMatcher(None, sound_fold(a), sound_fold(b)).ratio() >= SIMILAR
    )


def names_match(said: str, on_file: str) -> bool:
    """Whether the name the caller gave is the name on file: the whole name, or the
    first name when only that was given. Small mishearings are allowed, and the name
    may be said in English while it is stored in Hindi or Gujarati, or spelled out."""
    given, filed = _words(said), _words(on_file)
    if not given or not filed:
        return False
    if len(given) == 1:
        # One word may also be the whole name spelled without a break.
        return _close(given[0], filed[0]) or _close(given[0], "".join(filed))
    return _close(given[0], filed[0]) and _close(given[-1], filed[-1])


def appointment_number(said: str) -> int | None:
    """The number in "A-3", "a 3", "A three" or "3"; None when there is none."""
    words = re.findall(r"[a-z]+|\d+", said.casefold())
    digits = "".join(
        word if word.isdigit() else _SPOKEN_DIGITS.get(word, "") for word in words if word != "a"
    )
    if not digits or len(digits) > 7:
        return None
    number = int(digits)
    return number or None
