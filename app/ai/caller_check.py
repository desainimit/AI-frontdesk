"""The receptionist's check before a caller's appointments are shown or changed.

A caller ID can be faked, so on a call a matching number alone does not open someone's
appointments (unless the clinic trusts numbers). Instead, as a person at the desk would:
- calling from the patient's own number, they say the name on the appointment;
- calling from any other number, they give the appointment number (A-3) and the name.
Every move or cancellation also sends a message to the patient's own phone.
"""

import re
import unicodedata
from difflib import SequenceMatcher

from app.ai.romanize import latin_fold, sound_fold
from app.languages import pack

# Not part of a name when said aloud.
_TITLES = frozenset(
    {"mr", "mrs", "ms", "miss", "dr", "doctor", "shri", "smt", "ji", "sir", "madam"}
)


def _spoken_digits(language: str) -> dict[str, str]:
    """Digits as English and the call's language say them ("three", "teen", "ત્રણ").

    Only those two: "um" is "one" in Portuguese and a pause in English.
    """
    digits: dict[str, str] = {}
    for item in (pack("en"), pack(language)):
        for value, words in enumerate(item.digit_words):
            digits |= dict.fromkeys(words, str(value))
    return digits


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


def appointment_number(said: str, language: str = "en") -> int | None:
    """The number in "A-3", "a 3", "A three", "A teen", "A-३" or "3"; None when there
    is none. Digits of any script count."""
    spoken = _spoken_digits(language)
    # Indic vowel signs are not word characters to `\w`; they belong to the word.
    words = re.findall(r"[\wऀ-૿]+", unicodedata.normalize("NFC", said.casefold()))
    digits = "".join(
        "".join(str(unicodedata.decimal(c)) for c in word)
        if word.isdecimal()
        else spoken.get(word, "")
        for word in words
        if word != "a"
    )
    if not digits or len(digits) > 7:
        return None
    number = int(digits)
    return number or None
