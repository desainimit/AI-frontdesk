"""The little words a listener says to show they are following.

"Mm-hmm", "okay", "haan", "ji": said while the receptionist is talking, they mean
"go on", not "stop". They never cut the receptionist off, and on their own they are
not a new question.
"""

import re
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


# Said to end a call: "bye", "thank you", "you can cut the call", "rakh do".
_CLOSING = {
    "bye",
    "byebye",
    "bye-bye",
    "goodbye",
    "thanks",
    "thank",
    "cut",
    "hang",
    "disconnect",
    "namaste",
    "namaskar",
    "dhanyavaad",
    "dhanyawad",
    "shukriya",
    "rakh",
    "rakho",
    "band",
    "नमस्ते",
    "नमस्कार",
    "धन्यवाद",
    "शुक्रिया",
    "रख",
    "रखो",
    "बंद",
    "बाय",
    "कट",
    "આવજો",
    "આભાર",
    "બાય",
    "ok",
    "okay",
    "theek",
    "ठीक",
    "alright",
    "fine",
}
# The caller is not done after all.
_CARRY_ON = {
    "wait",
    "one",
    "more",
    "also",
    "but",
    "question",
    "sorry",
    "ruko",
    "rukiye",
    "aur",
    "ek",
    "रुको",
    "रुकिए",
    "और",
    "एक",
    "लेकिन",
    "પણ",
    "રાહ",
    "બીજું",
}
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
_ENDING = re.compile(
    r"\b(?:bye|bye-bye|goodbye|good bye|cut the call|cut the phone|hang up|disconnect"
    r"|that'?s all|that is all|that'?s it|nothing else|nothing more|i'?m done"
    r"|rakh(?:ta|ti|te)? (?:hoon|hu|hun|do|dijiye|rahe)|phone rakh\w*|band kar\w*"
    r"|bas itna|aur kuch nahi)\b"
    r"|बाय|फ़ोन रख|फोन रख|रखता हूँ|रखती हूँ|बस इतना|और कुछ नहीं|આવજો|ફોન મૂકું",
    re.IGNORECASE,
)
# The receptionist's "anything else?", after which "no, thanks" means goodbye.
_ANYTHING_ELSE = re.compile(
    r"anything else|kuch aur|कुछ और|બીજું કંઈ|algo más|autre chose|noch etwas|mais alguma",
    re.IGNORECASE,
)
# All a caller says to "anything else?" when they are done: "no, thanks", "that's all",
# "nahi bas", "thank you so much".
_DONE_WORDS = {
    "no", "nope", "nah", "nahi", "nahin", "thanks", "thank", "you", "so", "much", "very",
    "that's", "thats", "all", "it", "i'm", "im", "good", "fine", "ok", "okay", "nothing",
    "else", "bas", "ji", "dhanyavaad", "dhanyawad", "shukriya", "नहीं", "बस", "जी",
    "धन्यवाद", "शुक्रिया", "ना", "આભાર",
}  # fmt: skip
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
    known = _ALWAYS | _WORDS.get(base_language(language), set())
    return all(word in known for word in words)
