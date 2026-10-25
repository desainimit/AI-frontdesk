"""The little words a listener says to show they are following.

"Mm-hmm", "okay", "haan", "ji": said while the receptionist is talking, they mean
"go on", not "stop". They never cut the receptionist off, and on their own they are
not a new question.
"""

import unicodedata

from app.conversation.locales import base_language

_COMMON = {
    "mm",
    "mmm",
    "mhm",
    "mmhmm",
    "mm-hmm",
    "mm-hm",
    "uh-huh",
    "uhhuh",
    "uh",
    "um",
    "hmm",
    "hm",
    "ah",
    "oh",
    "ok",
    "okay",
}
_WORDS: dict[str, set[str]] = {
    "en": {
        "yeah",
        "yes",
        "yep",
        "yup",
        "right",
        "sure",
        "alright",
        "all",
        "got",
        "it",
        "i",
        "see",
        "cool",
        "great",
        "fine",
        "good",
        "uh",
    },
    "hi": {
        "haan",
        "han",
        "ha",
        "ji",
        "jee",
        "accha",
        "acha",
        "achha",
        "theek",
        "thik",
        "hai",
        "हाँ",
        "हां",
        "जी",
        "अच्छा",
        "ठीक",
        "है",
        "हम्म",
    },
    "gu": {"હા", "હાં", "જી", "બરાબર", "સારું", "હમ્મ", "હં"},
    "es": {"sí", "si", "vale", "claro", "ajá", "aja", "ya", "bueno"},
    "fr": {"oui", "ouais", "daccord", "d'accord", "voilà", "bien"},
    "de": {"ja", "genau", "gut", "klar", "stimmt"},
    "pt": {"sim", "tá", "ta", "certo", "uhum", "claro", "beleza"},
}
# Hindi and English are mixed on the same call so often that both always count.
_ALWAYS = _COMMON | _WORDS["en"] | _WORDS["hi"]
MAX_WORDS = 3


def _words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    cleaned = "".join(" " if c in ',.!?;:।"' else c for c in normalized)
    edges = "'\u2019-"
    return [word.strip(edges) for word in cleaned.split() if word.strip(edges)]


def is_backchannel(text: str, language: str = "en") -> bool:
    words = _words(text)
    if not words or len(words) > MAX_WORDS:
        return False
    known = _ALWAYS | _WORDS.get(base_language(language), set())
    return all(word in known for word in words)
