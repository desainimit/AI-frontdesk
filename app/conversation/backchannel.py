"""The little words a listener says to show they are following.

"Mm-hmm", "okay", "haan", "ji": said while the receptionist is talking, they mean
"go on", not "stop". They never cut the receptionist off, and on their own they are
not a new question.
"""

import re
import unicodedata

from app.languages import CODES, all_packs, normalize, pack

_WORDS: dict[str, frozenset[str]] = {code: pack(code).backchannel for code in CODES}
# Hindi, Gujarati and English are mixed on Indian calls so often that all always count.
_ALWAYS = _WORDS["en"] | _WORDS["hi"] | _WORDS["hi-Latn"] | _WORDS["gu"]
MAX_WORDS = 3
# Someone checking the line is there, not saying anything to answer.
_LINE_CHECK = frozenset(
    {"allo", "hallo", "hello", "helo", "hey", "hi", "हलो", "हाय", "हेलो", "हैलो", "હલો", "હાય", "હેલો"}
)
# A word that stops the receptionist on its own; one sound or stray word does not.
INTERRUPT_WORDS = 2


def _words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    cleaned = "".join(" " if c in ',.!?;:।"' else c for c in normalized)
    edges = "'\u2019-"
    # "Hmmmm" is "hmm".
    return [
        re.sub(r"(.)\1{2,}$", r"\1\1", word.strip(edges))
        for word in cleaned.split()
        if word.strip(edges)
    ]


# Said to end a call ("bye", "thank you", "you can cut the call", "rakh do"), and the
# caller not being done after all ("wait", "one more"), in any language.
_CLOSING = frozenset[str]().union(*(item.closing for item in all_packs()))
_CARRY_ON = frozenset[str]().union(*(item.carry_on for item in all_packs()))
MAX_CLOSING_WORDS = 6


def is_closing(text: str, language: str = "en") -> bool:
    """A reply after the receptionist's goodbye that still ends the call: a thank-you,
    a bye, "you can cut it", or a short sound the recognizer half-heard."""
    words = _words(text)
    if not words or "?" in text or any(word in _CARRY_ON for word in words):
        return False
    if any(word in _CLOSING for word in words):
        return True
    return is_backchannel(text, language) or len(words) <= 2


# The caller is ending the call: "bye", "you can cut the call", "that's all",
# "phone rakhta hoon". Said with nothing else to ask, it needs no reply but a goodbye.
_ENDING = re.compile("|".join(item.ending for item in all_packs()), re.IGNORECASE)
# The receptionist's "anything else?", after which "no, thanks" means goodbye.
_ANYTHING_ELSE = re.compile("|".join(item.anything_else for item in all_packs()), re.IGNORECASE)
# All a caller says to "anything else?" when they are done: "no, thanks", "that's all",
# "nahi bas", "thank you so much".
_DONE_WORDS = frozenset[str]().union(*(item.done for item in all_packs()))
# An explicit goodbye inside a longer request ("I need to cancel, bye") is not one.
MAX_ENDING_WORDS = 10


def wants_to_end(text: str, last_reply: str | None = None) -> bool:
    """Whether the caller is saying goodbye, with nothing more to ask.

    An explicit goodbye ("bye", "cut the call", "phone rakhta hoon"), or "no, thanks"
    straight after the receptionist asked whether there is anything else.
    """
    if "?" in text or "?" in text:
        return False
    words = _words(text)
    if not words or any(word in _CARRY_ON for word in words):
        return False
    if _ENDING.search(text) and len(words) <= MAX_ENDING_WORDS:
        return True
    asked = bool(last_reply and _ANYTHING_ELSE.search(last_reply))
    return asked and len(words) <= MAX_CLOSING_WORDS and all(word in _DONE_WORDS for word in words)


def is_backchannel(text: str, language: str = "en") -> bool:
    words = _words(text)
    if not words or len(words) > MAX_WORDS:
        return False
    known = _known(language)
    return all(word in known for word in words)


def _known(language: str) -> frozenset[str]:
    return _ALWAYS | _WORDS.get(normalize(language), frozenset())


def is_line_check(text: str) -> bool:
    """ "Hello?", "Hey?": the caller is asking whether anyone is there."""
    words = _words(text)
    return bool(words) and len(words) <= MAX_WORDS and all(word in _LINE_CHECK for word in words)


def interrupts(text: str, language: str = "en") -> bool:
    """Whether words said over the receptionist should stop her: "wait", or at least two
    real words. "Hmm", "ha ha", "hello" and one half-heard word ("Extra") do not."""
    words = _words(text)
    if any(word in _CARRY_ON for word in words):
        return True
    known = _known(language) | _LINE_CHECK
    return sum(word not in known for word in words) >= INTERRUPT_WORDS


# The receptionist's own goodbye, said without end_call being asked for.
_FAREWELL = re.compile(
    "|".join(item.farewell for item in all_packs() if item.farewell), re.IGNORECASE
)


def says_farewell(reply: str) -> bool:
    """Whether a reply of the receptionist's closes the call."""
    last = re.split(r"(?<=[.!?।])\s+", reply.strip())[-1]
    return "?" not in last and bool(_FAREWELL.search(last))
