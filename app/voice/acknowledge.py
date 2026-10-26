"""The word a receptionist says the moment the caller stops talking ("Okay,", "जी,").

A person answers within a beat, even when the actual answer takes a moment to think
of. The model needs a second or two, so a short acknowledgement fills that beat; the
model's reply then follows it without opening with another one.
"""

import re

from app.conversation.locales import normalize_language

# Varied, taken in turn; never one that promises a lookup (the model decides that).
ACKS: dict[str, tuple[str, ...]] = {
    "en": ("Okay,", "Sure,", "Alright,", "Mm-hmm,", "Got it,"),
    "hi": ("जी,", "अच्छा,", "ठीक है,", "हाँ जी,"),
    "hinglish": ("Ji,", "Achha,", "Theek hai,", "Haan ji,"),
    "gu": ("જી,", "સારું,", "હા,"),
    "es": ("Vale,", "Claro,", "Muy bien,"),
    "fr": ("D'accord,", "Très bien,", "Oui,"),
    "de": ("Okay,", "Gut,", "Alles klar,"),
    "pt": ("Certo,", "Claro,", "Tá bom,"),
}
# Said after a question from the caller: a thinking sound, not a "sure".
QUESTION_ACKS: dict[str, tuple[str, ...]] = {
    "en": ("Mm,", "So,", "Let's see,"),
    "hi": ("जी,", "अच्छा,"),
    "hinglish": ("Ji,", "Achha,"),
}

# Openers the model may start with that would repeat the acknowledgement just said.
# A plain "yes" is kept: it can be the answer itself.
_OPENERS: dict[str, str] = {
    "en": (
        r"sure|okay|ok|alright|all right|got it|of course|no problem|perfect|great|"
        r"absolutely|certainly|right|mm-hmm|mm|so|sounds good|lovely"
    ),
    "hi": r"जी हाँ|हाँ जी|जी ज़रूर|ज़रूर|ठीक है|अच्छा|जी",
    "hinglish": r"ji haan|haan ji|ji zaroor|zaroor|theek hai|achha|accha|ji",
    "gu": r"જી હા|જી|સારું|હા",
    "es": r"vale|claro|muy bien|perfecto|de acuerdo|sí",
    "fr": r"d'accord|très bien|oui|parfait|bien sûr",
    "de": r"okay|gut|alles klar|klar|ja|gerne",
    "pt": r"certo|claro|tá bom|perfeito|sim",
}
_PATTERNS = {
    code: re.compile(rf"^\s*(?:{words})\s*[,.!।]+\s*", re.IGNORECASE)
    for code, words in _OPENERS.items()
}

# Hurt, worried or upset: a breezy "Okay," would sound wrong; the reply starts itself.
_SERIOUS = re.compile(
    r"\b(pain|hurt|hurts|bleed|bleeding|emergency|accident|scared|worried|died|death|"
    r"can't breathe|chest|faint|fainted|unconscious|suicid\w*)\b|दर्द|खून|इमरजेंसी|"
    r"\b(dard|khoon|emergency)\b",
    re.IGNORECASE,
)


def acknowledgement(language: str, caller_text: str, turn: int, last: str | None) -> str | None:
    """The acknowledgement for this turn, or None when one would sound wrong."""
    if _SERIOUS.search(caller_text):
        return None
    code = normalize_language(language)
    question = caller_text.rstrip().endswith(("?", "?"))
    pool = (QUESTION_ACKS if question else ACKS).get(code) or ACKS.get(code) or ACKS["en"]
    choice = pool[turn % len(pool)]
    if choice == last and len(pool) > 1:
        choice = pool[(turn + 1) % len(pool)]
    return choice


def all_acknowledgements(language: str) -> tuple[str, ...]:
    code = normalize_language(language)
    lines = (*ACKS.get(code, ACKS["en"]), *QUESTION_ACKS.get(code, ()))
    return tuple(dict.fromkeys(lines))


def after_acknowledgement(text: str, language: str) -> str:
    """`text` without a leading "Sure," or "Okay," that would echo the acknowledgement.

    Empty when the sentence was nothing more than that.
    """
    pattern = _PATTERNS.get(normalize_language(language), _PATTERNS["en"])
    rest = pattern.sub("", text, count=1)
    if rest == text:
        return text
    rest = rest.strip()
    return rest[:1].upper() + rest[1:] if rest else ""
