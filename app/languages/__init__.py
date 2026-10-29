"""The languages the receptionist works in: one registry, one pack per language.

Every part of the product that depends on the language reads it from here: what is
said (the packs), how speech is recognised and spoken (the speech fields), how a
language is recognised in text (script, Latin words, Lingua). A language is listed
only when its pack is complete (tests/test_language_packs.py).

Codes are BCP-47. Hinglish is Hindi written in Latin letters, "hi-Latn": one spoken
language with Hindi, so a call never switches between the two.
"""

from dataclasses import dataclass
from functools import cache
from importlib import import_module

from app.languages.types import ClockWords as ClockWords
from app.languages.types import LanguagePack


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    native_name: str
    # The spoken language; Hinglish is Hindi.
    family: str
    # The Unicode block it is written in, when not Latin letters.
    script: tuple[str, str] | None
    # Lingua's name for it, for telling Latin-script languages apart.
    lingua: str | None
    # Speech locale by the clinic's country, and the one used elsewhere.
    locales: dict[str, str]
    default_locale: str
    # One natural Azure neural voice per locale.
    azure_voices: dict[str, str]
    # Deepgram's multilingual Flux model understands it.
    flux: bool
    # Sarvam's code, for the Indian languages it is built for.
    sarvam: str | None
    # The language code ElevenLabs is told, when it should be told one.
    elevenlabs: str | None
    # ElevenLabs models that speak it.
    elevenlabs_models: frozenset[str]

    def locale(self, country: str | None = None) -> str:
        return self.locales.get(country or "", self.default_locale)

    def azure_voice(self, country: str | None = None) -> str:
        return self.azure_voices[self.locale(country)]


_EVERY_ELEVENLABS = frozenset(
    {"eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2", "eleven_v3"}
)

LANGUAGES: dict[str, Language] = {
    "en": Language(
        "en",
        "English",
        "English",
        "en",
        None,
        "ENGLISH",
        {"IN": "en-IN", "GB": "en-GB", "AU": "en-AU", "US": "en-US"},
        "en-US",
        {
            "en-US": "en-US-JennyNeural",
            "en-IN": "en-IN-NeerjaNeural",
            "en-GB": "en-GB-SoniaNeural",
            "en-AU": "en-AU-NatashaNeural",
        },
        flux=True,
        sarvam="en-IN",
        elevenlabs="en",
        elevenlabs_models=_EVERY_ELEVENLABS,
    ),
    "hi": Language(
        "hi",
        "Hindi",
        "हिंदी",
        "hi",
        ("ऀ", "ॿ"),
        None,
        {},
        "hi-IN",
        {"hi-IN": "hi-IN-SwaraNeural"},
        flux=True,
        sarvam="hi-IN",
        elevenlabs="hi",
        elevenlabs_models=_EVERY_ELEVENLABS,
    ),
    "hi-Latn": Language(
        "hi-Latn",
        "Hinglish",
        "Hinglish",
        "hi",
        None,
        None,
        {},
        "hi-IN",
        {"hi-IN": "hi-IN-SwaraNeural"},
        flux=True,
        sarvam="hi-IN",
        # Read with Hindi pronunciation, but not marked as Hindi text.
        elevenlabs=None,
        elevenlabs_models=_EVERY_ELEVENLABS,
    ),
    "gu": Language(
        "gu",
        "Gujarati",
        "ગુજરાતી",
        "gu",
        ("઀", "૿"),
        None,
        {},
        "gu-IN",
        {"gu-IN": "gu-IN-DhwaniNeural"},
        flux=False,
        sarvam="gu-IN",
        elevenlabs="gu",
        elevenlabs_models=frozenset({"eleven_v3"}),
    ),
    "es": Language(
        "es",
        "Spanish",
        "Español",
        "es",
        None,
        "SPANISH",
        {"MX": "es-MX", "US": "es-US", "ES": "es-ES"},
        "es-ES",
        {
            "es-ES": "es-ES-ElviraNeural",
            "es-MX": "es-MX-DaliaNeural",
            "es-US": "es-US-PalomaNeural",
        },
        flux=True,
        sarvam=None,
        elevenlabs="es",
        elevenlabs_models=_EVERY_ELEVENLABS,
    ),
    "fr": Language(
        "fr",
        "French",
        "Français",
        "fr",
        None,
        "FRENCH",
        {"CA": "fr-CA", "FR": "fr-FR"},
        "fr-FR",
        {"fr-FR": "fr-FR-DeniseNeural", "fr-CA": "fr-CA-SylvieNeural"},
        flux=True,
        sarvam=None,
        elevenlabs="fr",
        elevenlabs_models=_EVERY_ELEVENLABS,
    ),
    "de": Language(
        "de",
        "German",
        "Deutsch",
        "de",
        None,
        "GERMAN",
        {"DE": "de-DE", "AT": "de-AT", "CH": "de-CH"},
        "de-DE",
        {"de-DE": "de-DE-KatjaNeural", "de-AT": "de-AT-IngridNeural", "de-CH": "de-CH-LeniNeural"},
        flux=True,
        sarvam=None,
        elevenlabs="de",
        elevenlabs_models=_EVERY_ELEVENLABS,
    ),
    "pt": Language(
        "pt",
        "Portuguese",
        "Português",
        "pt",
        None,
        "PORTUGUESE",
        {"BR": "pt-BR", "PT": "pt-PT"},
        "pt-BR",
        {"pt-BR": "pt-BR-FranciscaNeural", "pt-PT": "pt-PT-RaquelNeural"},
        flux=True,
        sarvam=None,
        elevenlabs="pt",
        elevenlabs_models=_EVERY_ELEVENLABS,
    ),
}
CODES: tuple[str, ...] = tuple(LANGUAGES)
_MODULES = {"hi-Latn": "hi_latn"}
# Older names for the same languages, as saved settings and conversations may hold them.
_ALIASES = {"hinglish": "hi-Latn", "hi-latn": "hi-Latn", "guj": "gu"}


def normalize(value: str) -> str:
    """The registry code for a language tag ("hi-IN" -> "hi", "hinglish" -> "hi-Latn",
    "en_GB" -> "en"). A tag of an unlisted language keeps its primary subtag."""
    tag = value.strip().replace("_", "-")
    lowered = tag.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    primary = lowered.split("-")[0]
    return _ALIASES.get(primary, primary)


def known(value: str) -> bool:
    return normalize(value) in LANGUAGES


def language(value: str) -> Language:
    """The registry entry for a code; KeyError for a language that is not offered."""
    return LANGUAGES[normalize(value)]


def family(value: str) -> str:
    """The spoken language: Hinglish is Hindi."""
    code = normalize(value)
    entry = LANGUAGES.get(code)
    return entry.family if entry else code


@cache
def pack(value: str) -> LanguagePack:
    """Everything said and listened for in a language. An unlisted code gets English:
    a conversation's language is always a listed one, so this only guards old data."""
    code = normalize(value)
    if code not in LANGUAGES:
        code = "en"
    return import_module(f"app.languages.packs.{_MODULES.get(code, code)}").PACK


def all_packs() -> tuple[LanguagePack, ...]:
    return tuple(pack(code) for code in CODES)
