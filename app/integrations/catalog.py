"""Server-owned catalog of supported provider connections.

Tenants choose from this allowlist only. There are deliberately no tenant-supplied base
URLs, hosts or headers: every outbound provider address is derived from typed fields.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator


class Purpose(StrEnum):
    TELEPHONY = "telephony"
    STT = "stt"
    TTS = "tts"
    LLM = "llm"
    EMBEDDINGS = "embeddings"


class ConnectionSlot(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"


# The region becomes part of a provider hostname, so it is a closed list.
AzureRegion = Literal[
    "australiaeast",
    "brazilsouth",
    "canadacentral",
    "centralindia",
    "centralus",
    "eastasia",
    "eastus",
    "eastus2",
    "francecentral",
    "germanywestcentral",
    "japaneast",
    "koreacentral",
    "northcentralus",
    "northeurope",
    "southcentralus",
    "southeastasia",
    "swedencentral",
    "switzerlandnorth",
    "uaenorth",
    "uksouth",
    "westcentralus",
    "westeurope",
    "westus",
    "westus2",
    "westus3",
]
_VOICE = r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z0-9]{2,40}Neural$"


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _Secrets(BaseModel):
    # hide_input_in_errors: a rejected secret must never be echoed by validation.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EmptyConfig(_Config):
    pass


class DeepgramConfig(_Config):
    model: Literal["nova-2", "nova-3"] = "nova-3"
    language: Literal["multi", "en", "hi", "es", "fr", "de", "pt"] = "multi"

    @model_validator(mode="after")
    def multilingual_model(self) -> Self:
        if self.language == "multi" and self.model != "nova-3":
            raise ValueError("Multilingual transcription requires nova-3")
        return self


class AzureSpeechToTextConfig(_Config):
    region: AzureRegion = "eastus"
    language: str = Field(default="en-US", pattern=r"^[a-z]{2,3}-[A-Z]{2}$")


class AzureTextToSpeechConfig(_Config):
    region: AzureRegion = "eastus"
    voice: str = Field(default="en-US-JennyNeural", pattern=_VOICE)
    hindi_voice: str = Field(default="hi-IN-SwaraNeural", pattern=_VOICE)


class ElevenLabsConfig(_Config):
    model_id: Literal["eleven_turbo_v2_5", "eleven_multilingual_v2"] = "eleven_turbo_v2_5"
    voice_id: str = Field(default="21m00Tcm4TlvDq8ikWAM", pattern=r"^[A-Za-z0-9]{20}$")


class GeminiChatConfig(_Config):
    # The platform allowlist (TENANT_LLM_MODELS) is enforced by the service.
    model: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]{1,62}$")
    # Lower levels answer sooner; None omits it for models that do not accept one.
    thinking_level: Literal["low", "medium", "high"] | None = "low"


class GeminiEmbeddingsConfig(_Config):
    # The vector index is fixed at 1536 dimensions; another size needs a rebuild.
    model: Literal["gemini-embedding-2"] = "gemini-embedding-2"


class TwilioSecrets(_Secrets):
    auth_token: SecretStr = Field(min_length=32, max_length=32)
    api_key_sid: str | None = Field(default=None, pattern=r"^SK[0-9a-fA-F]{32}$")
    api_key_secret: SecretStr | None = Field(default=None, min_length=16, max_length=128)

    @model_validator(mode="after")
    def complete(self) -> Self:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", self.auth_token.get_secret_value()):
            raise ValueError("Invalid Twilio auth token format")
        if (self.api_key_sid is None) != (self.api_key_secret is None):
            raise ValueError("Provide both the API key SID and secret, or neither")
        if self.api_key_secret is not None and not _printable(self.api_key_secret):
            raise ValueError("Invalid API key secret")
        return self


class ApiKeySecrets(_Secrets):
    api_key: SecretStr = Field(min_length=16, max_length=256)

    @model_validator(mode="after")
    def printable(self) -> Self:
        if not _printable(self.api_key):
            raise ValueError("Invalid API key")
        return self


class AzureSecrets(_Secrets):
    speech_key: SecretStr = Field(min_length=16, max_length=128)

    @model_validator(mode="after")
    def printable(self) -> Self:
        if not _printable(self.speech_key):
            raise ValueError("Invalid speech key")
        return self


def _printable(value: SecretStr) -> bool:
    # Header-safe: no whitespace, control characters or non-ASCII.
    return re.fullmatch(r"[\x21-\x7e]+", value.get_secret_value()) is not None


@dataclass(frozen=True)
class ProviderSpec:
    provider: str
    purpose: Purpose
    label: str
    config: type[_Config]
    secrets: type[_Secrets]
    slots: tuple[ConnectionSlot, ...] = (ConnectionSlot.PRIMARY,)
    account_pattern: str | None = None
    # Config that selects the provider endpoint: fixed while a credential exists.
    locked_fields: frozenset[str] = frozenset()
    suffix_field: str = "api_key"


_BOTH = (ConnectionSlot.PRIMARY, ConnectionSlot.FALLBACK)
_SPECS = (
    ProviderSpec(
        "twilio",
        Purpose.TELEPHONY,
        "Twilio",
        EmptyConfig,
        TwilioSecrets,
        account_pattern=r"^AC[0-9a-fA-F]{32}$",
        suffix_field="auth_token",
    ),
    ProviderSpec("deepgram", Purpose.STT, "Deepgram", DeepgramConfig, ApiKeySecrets, _BOTH),
    ProviderSpec(
        "azure_speech",
        Purpose.STT,
        "Azure Speech",
        AzureSpeechToTextConfig,
        AzureSecrets,
        _BOTH,
        locked_fields=frozenset({"region"}),
        suffix_field="speech_key",
    ),
    ProviderSpec("elevenlabs", Purpose.TTS, "ElevenLabs", ElevenLabsConfig, ApiKeySecrets, _BOTH),
    ProviderSpec(
        "azure_speech",
        Purpose.TTS,
        "Azure Speech",
        AzureTextToSpeechConfig,
        AzureSecrets,
        _BOTH,
        locked_fields=frozenset({"region"}),
        suffix_field="speech_key",
    ),
    ProviderSpec("gemini", Purpose.LLM, "Google Gemini", GeminiChatConfig, ApiKeySecrets),
    ProviderSpec(
        "gemini", Purpose.EMBEDDINGS, "Google Gemini", GeminiEmbeddingsConfig, ApiKeySecrets
    ),
)
CATALOG: dict[tuple[Purpose, str], ProviderSpec] = {
    (spec.purpose, spec.provider): spec for spec in _SPECS
}


class InvalidIntegrationError(ValueError):
    """Static, secret-free reason; safe to map to a 422."""


def spec_for(purpose: str, provider: str) -> ProviderSpec:
    try:
        return CATALOG[(Purpose(purpose), provider)]
    except (KeyError, ValueError):
        raise InvalidIntegrationError("Unsupported provider for this purpose") from None


def validate_config(spec: ProviderSpec, config: dict[str, Any]) -> dict[str, Any]:
    try:
        return spec.config.model_validate(config).model_dump(mode="json")
    except ValidationError:
        raise InvalidIntegrationError("Invalid provider configuration") from None


def validate_secrets(spec: ProviderSpec, secrets: dict[str, Any]) -> dict[str, str]:
    """Return plain values for encryption. Errors never include the submitted input."""
    try:
        model = spec.secrets.model_validate(secrets)
    except ValidationError:
        raise InvalidIntegrationError("Invalid provider credentials") from None
    result: dict[str, str] = {}
    for name in spec.secrets.model_fields:
        value = getattr(model, name)
        if value is not None:
            result[name] = value.get_secret_value() if isinstance(value, SecretStr) else value
    return result


class ProviderCatalogEntry(BaseModel):
    provider: str
    purpose: Purpose
    label: str
    slots: list[ConnectionSlot]
    requires_account_identifier: bool
    config_schema: dict[str, Any]
    secret_fields: list[str]
    required_secret_fields: list[str]


def catalog_entries(llm_models: tuple[str, ...]) -> list[ProviderCatalogEntry]:
    entries: list[ProviderCatalogEntry] = []
    for spec in _SPECS:
        schema = spec.config.model_json_schema()
        if spec.config is GeminiChatConfig:
            schema["properties"]["model"]["enum"] = list(llm_models)
        entries.append(
            ProviderCatalogEntry(
                provider=spec.provider,
                purpose=spec.purpose,
                label=spec.label,
                slots=list(spec.slots),
                requires_account_identifier=spec.account_pattern is not None,
                config_schema=schema,
                secret_fields=list(spec.secrets.model_fields),
                required_secret_fields=[
                    name for name, field in spec.secrets.model_fields.items() if field.is_required()
                ],
            )
        )
    return entries
