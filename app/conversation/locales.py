"""A clinic's language policy: which languages it offers and which it starts in.

What each language is and how it is spoken lives in the registry (app.languages).
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.languages import LANGUAGES, family, normalize

# The spoken languages a clinic can offer. Hinglish comes with Hindi.
LAUNCH_LANGUAGES = tuple(code for code, entry in LANGUAGES.items() if entry.family == code)
# Adding an opt-in language must not expand an omitted policy on existing documents.
DEFAULT_LANGUAGES = ("en", "es", "fr", "de", "pt", "hi")


def normalize_language(value: str) -> str:
    """The registry code for a language tag ("hinglish" -> "hi-Latn", "gu-IN" -> "gu")."""
    return normalize(value)


def base_language(value: str) -> str:
    """The spoken language: Hinglish is Hindi."""
    return family(value)


class LanguagePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_language: str = "en"
    enabled_languages: list[str] = Field(default_factory=lambda: list(DEFAULT_LANGUAGES))
    auto_detect: bool = True

    @field_validator("default_language")
    @classmethod
    def canonical_default(cls, value: str) -> str:
        # Any tag of an offered language ("hi-IN", "gu_IN") means that language.
        code = family(value)
        return code if code in LAUNCH_LANGUAGES else value

    @field_validator("enabled_languages")
    @classmethod
    def canonical_enabled(cls, values: list[str]) -> list[str]:
        return [cls.canonical_default(value) for value in values]

    @model_validator(mode="after")
    def validate_languages(self) -> Self:
        if not self.enabled_languages or len(self.enabled_languages) > len(LAUNCH_LANGUAGES):
            raise ValueError("Choose at least one supported language")
        if any(code not in LAUNCH_LANGUAGES for code in self.enabled_languages):
            raise ValueError(f"Supported languages: {', '.join(LAUNCH_LANGUAGES)}")
        if len(set(self.enabled_languages)) != len(self.enabled_languages):
            raise ValueError("Enabled languages must be unique")
        if self.default_language not in self.enabled_languages:
            raise ValueError("Default language must be enabled")
        return self
