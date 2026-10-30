"""Which language someone is speaking, read from their words.

The words decide, not the speech recogniser's language label: on Indian calls that
label was wrong both ways (Hindi lines labelled English on 27 Oct 18:13, an English
sentence labelled Hindi at 18:46). In order:
1. Hindi or Gujarati script: most words written in it.
2. Hindi or Gujarati in Latin letters: everyday words of that language ("mujhe kal
   appointment chahiye" is Hinglish; "I want an appointment" is not).
3. Other Latin-script languages: Lingua.
Short replies, names, numbers and repeated words say nothing about the language.

Hindi and Hinglish are one language for a call (`family`): only the script differs.
When the call language changes is decided by LanguageState; this module only reads.
"""

import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

from app.conversation.locales import LAUNCH_LANGUAGES
from app.languages import LANGUAGES, all_packs, family

# Latin-script languages told apart by Lingua, and the registry code of each.
_LINGUA = {
    getattr(Language, entry.lingua): code for code, entry in LANGUAGES.items() if entry.lingua
}
# Languages written in a script of their own: (code, first, last character).
_SCRIPTS = tuple(
    (code, entry.script[0], entry.script[1])
    for code, entry in LANGUAGES.items()
    if entry.script is not None
)
# Languages also written in Latin letters, with their everyday words in that form.
# Words that are also English ("to", "do", "main", "the", "par", "lie") are left out.
_LATIN_WORDS = tuple((item.code, item.latin_words) for item in all_packs() if item.latin_words)
# Names of the languages a caller may ask for, in any language they may ask in.
_NAMES: dict[str, tuple[str, ...]] = {}
for _item in all_packs():
    for _code, _names in _item.language_names.items():
        _NAMES[_code] = tuple(dict.fromkeys((*_NAMES.get(_code, ()), *_names)))
# Words that make naming a language a request to speak it ("Hindi mein bolo").
_ASKING: frozenset[str] = frozenset[str]().union(*(item.asking for item in all_packs()))
# A reply is checked once it has this many words; fewer say too little.
CHECK_WORDS = 4
# The Indian scripts, Devanagari to Malayalam.
INDIC_FIRST, INDIC_LAST = "\u0900", "\u0dff"


@dataclass(frozen=True)
class LanguageObservation:
    language: str
    confidence: float
    code_switching: bool = False


@lru_cache(maxsize=1)
def _detector() -> LanguageDetector:
    # Keep all candidates even for a one-language clinic: restricting to one language
    # would classify every sentence as that language.
    return LanguageDetectorBuilder.from_languages(*_LINGUA).build()


def _words(text: str) -> list[str]:
    """The words, letters and their vowel signs together ("हिंदी" is one word, not three)."""
    words = (
        "".join(c for c in token if c.isalpha() or unicodedata.category(c).startswith("M"))
        for token in text.casefold().replace("-", " ").split()
    )
    return [word for word in words if word]


def _script(words: list[str]) -> str | None:
    # Sarvam keeps English words in Latin letters, so a Hindi sentence full of "time" and
    # "appointment" may be under half Devanagari: two in five words decide it.
    for code, first, last in _SCRIPTS:
        hits = sum(any(first <= c <= last for c in word) for word in words)
        if hits >= 2 and hits * 5 >= len(words) * 2:
            return code
    return None


def _latin_indic(words: list[str]) -> tuple[str, float] | None:
    """Hinglish or Gujarati in Latin letters, from how many everyday words it has. They
    share words ("koi"): the one with the most is the one spoken."""
    hits, code = max(
        (sum(word in lexicon for word in words), code) for code, lexicon in _LATIN_WORDS
    )
    if hits >= 2 and hits * 4 >= len(words):
        return code, min(0.95, 0.6 + hits / len(words))
    return None


def observe_language(
    text: str,
    provider_language: str | None = None,
    *,
    provider_languages: tuple[str, ...] = (),
    enabled_languages: tuple[str, ...] = LAUNCH_LANGUAGES,
) -> LanguageObservation | None:
    """The language of what someone said, or None when it does not show.

    `provider_language(s)` are the recogniser's labels. They are accepted for the
    callers' convenience and deliberately not trusted (see the module docstring).
    """
    del provider_language, provider_languages
    words = _words(text)
    # Acknowledgements, names, codes and repeated words do not show a language.
    if len(words) < 3 or len(set(words)) < 2:
        return None
    latin = any("a" <= c <= "z" for c in text.casefold())
    written = _script(words)
    if written is not None:
        reading = LanguageObservation(written, 0.9, latin)
    elif (indic := _latin_indic(words)) is not None:
        reading = LanguageObservation(indic[0], indic[1], True)
    else:
        values = _detector().compute_language_confidence_values(text)
        if not values:
            return None
        best = values[0]
        if best.value < 0.6 or (len(values) > 1 and best.value - values[1].value < 0.15):
            return None
        reading = LanguageObservation(_LINGUA[best.language], best.value)
    if family(reading.language) not in enabled_languages:
        return None
    return reading


def asked_for_language(text: str, enabled_languages: tuple[str, ...]) -> str | None:
    """The language the person asks to speak ("Can we speak English?", "हिंदी में बोलिए",
    "Hindi please"), or None. Naming a language is a request when it comes with a word
    of asking or on its own in a short reply."""
    words = _words(text)
    named = [
        code
        for code, names in _NAMES.items()
        if family(code) in enabled_languages and any(name in words for name in names)
    ]
    if len(named) != 1:
        # None, or "Hindi or English?": not a clear request.
        return None
    if len(words) <= 3 or any(word in _ASKING for word in words):
        return named[0]
    return None


def _foreign_letters(text: str, language: str) -> bool:
    """Letters of another Indian script in a Hindi or Gujarati reply: on 30 Oct a Gujarati
    reply opened "કૃપા করে", with a Bengali word in it."""
    script = LANGUAGES[family(language)].script
    if script is None:
        return False
    first, last = script
    return any(INDIC_FIRST <= c <= INDIC_LAST and not first <= c <= last for c in text)


def reply_fits(text: str, language: str) -> bool:
    """Whether a reply is in the call's language. Too short to tell counts as yes."""
    if _foreign_letters(text, language):
        return False
    if len(_words(text)) < CHECK_WORDS:
        return True
    reading = observe_language(text)
    return reading is None or family(reading.language) == family(language)
