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

from app.conversation.locales import LAUNCH_LANGUAGES, base_language, normalize_language

_LATIN_LANGUAGES = (
    Language.ENGLISH,
    Language.SPANISH,
    Language.FRENCH,
    Language.GERMAN,
    Language.PORTUGUESE,
)
_SCRIPTS = (("hi", "\u0900", "\u097f"), ("gu", "\u0a80", "\u0aff"))


def _listed(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Everyday Hindi and Gujarati words as they come out in Latin letters. Words that are
# also English ("to", "do", "main", "the", "par", "lie") are left out.
_HINDI_WORDS = _listed(
    """
    hai hain tha thi ho hoga hogi hona ka ki ke ko se mein mai lekin aur bhi toh nahi
    nahin kya kyun kyon kab kaise kaisa kitna kitne kahan kaun kuch koi sab bas sirf
    mujhe mujhko mera meri mere hum hamara hamari hamare humko aap aapka aapki aapke
    aapko tum tumhara yeh ye woh vo wo unka unki inka chahiye chahta chahti karna karni
    karne karo kar kardo kariye kijiye dijiye dena dedo lena milna milega milegi mil
    sakta sakti sakte sakenge hoon hu haan han ji jee accha achha acha theek thik thoda
    zara jaldi abhi kal aaj parso baje bje subah sham shaam dopahar raat din hafte
    mahina samay wala wali wale liye saath pehle baad wahi matlab batao bataiye boliye
    bolo dekhiye dekho chalega chalo gaya gayi gaye raha rahi rahe hua hui baat zaroor
    jarur bilkul dhanyavaad shukriya namaste kripya
    """
)
_GUJARATI_WORDS = _listed(
    """
    chhe che nathi hatu hati hata maare mare mane mara mari maru tame tamne tamaru
    tamari tamara amne amaru kem shu shun kyare kyan kayu karvu karvi karvo karo joie
    joiye jove aavjo aavu saru barabar haa aaje kale savare saanje bapore vagye vage ane
    thi sathe pachhi pehla
    """
)
# Names of the languages a caller may ask for, in any language they may ask in.
_NAMES = {
    "en": ("english", "angrezi", "angreji", "अंग्रेज़ी", "अंग्रेजी", "इंग्लिश", "ઇંગ્લિશ", "અંગ્રેજી"),
    "hi": ("hindi", "हिंदी", "हिन्दी", "હિન્દી", "હિંદી"),
    "gu": ("gujarati", "gujrati", "ગુજરાતી", "गुजराती"),
    "es": ("spanish", "español", "espanol"),
    "fr": ("french", "français", "francais"),
    "de": ("german", "deutsch"),
    "pt": ("portuguese", "português", "portugues"),
}  # fmt: skip
# Words that make naming a language a request to speak it ("Hindi mein bolo").
_ASKING = _listed(
    """
    speak talk say language please can could prefer switch reply answer continue mein me
    bolo boliye baat kariye karo karein chalega samjha samajh में बोलो बोलिए बात करो
    कीजिए समझ भाषा માં બોલો વાત કરો ભાષા habla hablar idioma parlez parler langue
    sprechen sprache fala falar
    """
)
# A reply is checked once it has this many words; fewer say too little.
CHECK_WORDS = 4


@dataclass(frozen=True)
class LanguageObservation:
    language: str
    confidence: float
    code_switching: bool = False


@lru_cache(maxsize=1)
def _detector() -> LanguageDetector:
    # Keep all candidates even for a one-language clinic: restricting to one language
    # would classify every sentence as that language.
    return LanguageDetectorBuilder.from_languages(*_LATIN_LANGUAGES).build()


def family(code: str) -> str:
    """The spoken language: Hinglish is Hindi written in Latin letters."""
    return base_language(normalize_language(code))


def _words(text: str) -> list[str]:
    """The words, letters and their vowel signs together ("हिंदी" is one word, not three)."""
    words = (
        "".join(c for c in token if c.isalpha() or unicodedata.category(c).startswith("M"))
        for token in text.casefold().replace("-", " ").split()
    )
    return [word for word in words if word]


def _script(words: list[str]) -> str | None:
    for code, first, last in _SCRIPTS:
        if sum(any(first <= c <= last for c in word) for word in words) * 2 > len(words):
            return code
    return None


def _latin_indic(words: list[str]) -> tuple[str, float] | None:
    """Hinglish or Gujarati in Latin letters, from how many everyday words it has."""
    for code, lexicon in (("hinglish", _HINDI_WORDS), ("gu", _GUJARATI_WORDS)):
        hits = sum(word in lexicon for word in words)
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
        code = best.language.iso_code_639_1.name.lower()
        reading = LanguageObservation(code, best.value)
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
        if code in enabled_languages and any(name in words for name in names)
    ]
    if len(named) != 1:
        # None, or "Hindi or English?": not a clear request.
        return None
    if len(words) <= 3 or any(word in _ASKING for word in words):
        return named[0]
    return None


def reply_fits(text: str, language: str) -> bool:
    """Whether a reply is in the call's language. Too short to tell counts as yes."""
    if len(_words(text)) < CHECK_WORDS:
        return True
    reading = observe_language(text)
    return reading is None or family(reading.language) == family(language)
