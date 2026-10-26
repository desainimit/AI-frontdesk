"""Whether the caller has finished their sentence or only paused in the middle of it.

Speech recognition ends a turn after a short silence, and a caller who stops to think
("I'd like to check my... appointment") would be answered half-way. Words that cannot
end a sentence ("my", "the", "about", "और", "ke") mean they are still going: the
receptionist waits a moment for the rest instead of answering the fragment.
"""

import re

_ENGLISH = {
    "a", "an", "the", "to", "for", "of", "and", "or", "but", "with", "my", "your", "his",
    "her", "our", "their", "about", "from", "at", "by", "because", "if", "than", "into",
    "i", "i'm", "we're", "it's", "there's", "very", "um", "uh", "er", "erm", "like",
}  # fmt: skip
_HINDI = {
    "aur", "ki", "ke", "ka", "ko", "se", "mein", "par", "toh", "lekin", "kyunki",
    "agar", "matlab", "jo", "ek", "और", "कि", "के", "की", "का", "को", "से", "में", "पर",
    "तो", "लेकिन", "या", "क्योंकि", "अगर", "मतलब", "जो", "एक",
}  # fmt: skip
# Callers mix languages within a sentence, so both lists apply whatever the call's
# language ("ya" is left out: in English it is "yeah").
_WORDS = _ENGLISH | _HINDI
# "I was thinking...", "so, um," or "and -": trailing off, not finished.
_TRAILING = re.compile(r"(?:\.\.\.|…|,|-|—)\s*$")
_LAST_WORD = re.compile(r"([\w'ऀ-ॿ]+)[\s\"')\]]*$")


def sounds_unfinished(text: str) -> bool:
    text = text.strip()
    if not text or text.endswith(("?", "!", "?")):
        return False
    if _TRAILING.search(text):
        return True
    match = _LAST_WORD.search(text.rstrip(".।"))
    if match is None:
        return False
    return match[1].lower() in _WORDS
