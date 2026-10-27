"""STT language observations with Lingua as the secondary text classifier."""

from dataclasses import dataclass
from functools import lru_cache

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

from app.conversation.locales import LAUNCH_LANGUAGES, base_language, normalize_language

_LINGUA_LANGUAGES = (
    Language.ENGLISH,
    Language.SPANISH,
    Language.FRENCH,
    Language.GERMAN,
    Language.PORTUGUESE,
    Language.HINDI,
    Language.GUJARATI,
)


@dataclass(frozen=True)
class LanguageObservation:
    language: str
    confidence: float
    code_switching: bool = False


@lru_cache(maxsize=1)
def _detector() -> LanguageDetector:
    # Keep all launch candidates even for a monolingual shop: restricting to one
    # language would falsely classify every utterance as that language.
    return LanguageDetectorBuilder.from_languages(*_LINGUA_LANGUAGES).build()


def _written_in(text: str) -> str | None:
    """Hindi or Gujarati when most words are written in that script ("मुझे साढ़े दस की
    appointment चाहिए"); None otherwise, so a name alone does not count."""
    words = [word for word in text.split() if any(char.isalpha() for char in word)]
    for code, first, last in (("hi", "ऀ", "ॿ"), ("gu", "઀", "૿")):
        if words and sum(any(first <= c <= last for c in word) for word in words) * 2 > len(words):
            return code
    return None


def observe_language(
    text: str,
    provider_language: str | None = None,
    *,
    provider_languages: tuple[str, ...] = (),
    enabled_languages: tuple[str, ...] = LAUNCH_LANGUAGES,
) -> LanguageObservation | None:
    tokens = [
        "".join(char for char in token if char.isalpha()) for token in text.casefold().split()
    ]
    tokens = [token for token in tokens if token]
    # Generic evidence guard: acknowledgments, names, codes and repeated words
    # do not change the call's language, regardless of vocabulary.
    if len(tokens) < 3 or len(set(tokens)) < 2:
        return None
    written = _written_in(text)
    if written is not None:
        # Hindi or Gujarati script is that language, whatever the recogniser labelled it:
        # on 27 Oct "दस बजे का" came labelled English, and with one misheard English line
        # before it, the call switched to English while the caller went on in Hindi.
        if written not in enabled_languages:
            return None
        return LanguageObservation(written, 0.9, any("a" <= c <= "z" for c in text.casefold()))
    codes = tuple(
        dict.fromkeys(
            normalize_language(code)
            for code in ((provider_language,) if provider_language else ()) + provider_languages
            if code and code != "multi"
        )
    )
    if codes:
        primary = codes[0]
        if base_language(primary) not in enabled_languages:
            return None
        mixed = len(set(map(base_language, codes))) > 1
        latin_hindi = primary == "hi" and not any("\u0900" <= c <= "\u097f" for c in text)
        if primary == "hi" and (latin_hindi or "en" in codes):
            primary = "hinglish"
        return LanguageObservation(primary, 0.85, mixed or primary == "hinglish")

    values = _detector().compute_language_confidence_values(text)
    if not values:
        return None
    best = values[0]
    if best.value < 0.6 or (len(values) > 1 and best.value - values[1].value < 0.15):
        return None
    code = best.language.iso_code_639_1.name.lower()
    if code not in enabled_languages:
        return None
    spans = _detector().detect_multiple_languages_of(text)
    mixed = len({span.language for span in spans if span.word_count >= 2}) > 1
    return LanguageObservation(code, best.value, mixed)
