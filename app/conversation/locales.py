"""Launch-language capabilities and tenant policy, not vocabulary-based detection."""

from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


@dataclass(frozen=True)
class Locale:
    name: str
    speech_locale: str
    azure_voice: str


LOCALES = {
    "en": Locale("English", "en-US", "en-US-JennyNeural"),
    "es": Locale("Spanish", "es-ES", "es-ES-ElviraNeural"),
    "fr": Locale("French", "fr-FR", "fr-FR-DeniseNeural"),
    "de": Locale("German", "de-DE", "de-DE-KatjaNeural"),
    "pt": Locale("Portuguese", "pt-BR", "pt-BR-FranciscaNeural"),
    "hi": Locale("Hindi", "hi-IN", "hi-IN-SwaraNeural"),
    "gu": Locale("Gujarati", "gu-IN", "gu-IN-DhwaniNeural"),
}
LAUNCH_LANGUAGES = tuple(LOCALES)
# Adding an opt-in language must not expand an omitted policy on existing documents.
DEFAULT_LANGUAGES = ("en", "es", "fr", "de", "pt", "hi")


def normalize_language(value: str) -> str:
    code = value.strip().lower().replace("_", "-")
    if code in {"hinglish", "hi-latn"}:
        return "hinglish"
    language = code.split("-")[0]
    return "gu" if language == "guj" else language


def base_language(value: str) -> str:
    code = normalize_language(value)
    return "hi" if code == "hinglish" else code


class LanguagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_language: str = "en"
    enabled_languages: list[str] = Field(default_factory=lambda: list(DEFAULT_LANGUAGES))
    auto_detect: bool = True

    @field_validator("default_language")
    @classmethod
    def canonical_default(cls, value: str) -> str:
        return "gu" if value.strip().lower().replace("_", "-") in {"gu", "guj", "gu-in"} else value

    @field_validator("enabled_languages")
    @classmethod
    def canonical_enabled(cls, values: list[str]) -> list[str]:
        return [cls.canonical_default(value) for value in values]

    @model_validator(mode="after")
    def validate_languages(self) -> Self:
        if not self.enabled_languages or len(self.enabled_languages) > len(LOCALES):
            raise ValueError("Choose at least one supported language")
        if any(code not in LOCALES for code in self.enabled_languages):
            raise ValueError(f"Supported languages: {', '.join(LOCALES)}")
        if len(set(self.enabled_languages)) != len(self.enabled_languages):
            raise ValueError("Enabled languages must be unique")
        if self.default_language not in self.enabled_languages:
            raise ValueError("Default language must be enabled")
        return self
