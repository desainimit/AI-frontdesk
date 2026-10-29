"""The word a receptionist says the moment the caller stops talking ("Okay,", "जी,").

A person answers within a beat, even when the actual answer takes a moment to think
of. The model needs a second or two, so a short acknowledgement fills that beat; the
model's reply then follows it without opening with another one.
"""

import re

from app.languages import CODES, all_packs, normalize, pack

# Varied, taken in turn; never one that promises a lookup (the model decides that).
ACKS: dict[str, tuple[str, ...]] = {code: pack(code).acks for code in CODES}
# Said after a question from the caller: a thinking sound, not a "sure".
QUESTION_ACKS: dict[str, tuple[str, ...]] = {code: pack(code).question_acks for code in CODES}

# Openers the model may start with that would repeat the acknowledgement just said.
# A plain "yes" is kept: it can be the answer itself.
_PATTERNS = {
    code: re.compile(rf"^\s*(?:{pack(code).openers})\s*[,.!।]+\s*", re.IGNORECASE) for code in CODES
}

# Hurt, worried or upset, in any language: a breezy "Okay," would sound wrong.
_SERIOUS = re.compile("|".join(item.serious for item in all_packs()), re.IGNORECASE)


def acknowledgement(language: str, caller_text: str, turn: int, last: str | None) -> str | None:
    """The acknowledgement for this turn, or None when one would sound wrong."""
    if _SERIOUS.search(caller_text):
        return None
    words = pack(language)
    question = caller_text.rstrip().endswith(("?", "?"))
    pool = words.question_acks if question else words.acks
    choice = pool[turn % len(pool)]
    if choice == last and len(pool) > 1:
        choice = pool[(turn + 1) % len(pool)]
    return choice


def all_acknowledgements(language: str) -> tuple[str, ...]:
    words = pack(language)
    lines = (*words.acks, *words.question_acks)
    return tuple(dict.fromkeys(lines))


def after_acknowledgement(text: str, language: str) -> str:
    """`text` without a leading "Sure," or "Okay," that would echo the acknowledgement.

    Empty when the sentence was nothing more than that.
    """
    pattern = _PATTERNS.get(normalize(language), _PATTERNS["en"])
    rest = pattern.sub("", text, count=1)
    if rest == text:
        return text
    rest = rest.strip()
    return rest[:1].upper() + rest[1:] if rest else ""
