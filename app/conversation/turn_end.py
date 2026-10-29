"""Whether the caller has finished their sentence or only paused in the middle of it.

Speech recognition ends a turn after a short silence, and a caller who stops to think
("I'd like to check my... appointment") would be answered half-way. Words that cannot
end a sentence ("my", "the", "about", "और", "ke") mean they are still going: the
receptionist waits a moment for the rest instead of answering the fragment.
"""

import re

from app.languages import pack

# Callers mix languages within a sentence, so English and Hindi words always count
# besides the call's own language ("ya" is left out: in English it is "yeah").
_ALWAYS = pack("en").unfinished | pack("hi").unfinished | pack("hi-Latn").unfinished
# "I was thinking...", "so, um," or "and -": trailing off, not finished.
_TRAILING = re.compile(r"(?:\.\.\.|…|,|-|—)\s*$")
_LAST_WORD = re.compile(r"([\w'ऀ-ॿ઀-૿]+)[\s\"')\]]*$")


def sounds_unfinished(text: str, language: str = "en") -> bool:
    text = text.strip()
    if not text or text.endswith(("?", "!", "?")):
        return False
    if _TRAILING.search(text):
        return True
    match = _LAST_WORD.search(text.rstrip(".।"))
    if match is None:
        return False
    return match[1].lower() in _ALWAYS | pack(language).unfinished
