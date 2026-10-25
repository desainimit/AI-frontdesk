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
    EMAIL = "email"
    GOOGLE_CALENDAR = "google_calendar"
    MICROSOFT_CALENDAR = "microsoft_calendar"


class ConnectionSlot(StrEnum):
    PRIMARY = "primary"


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
    language: Literal["multi", "en", "hi", "es", "fr", "de", "pt", "gu"] = "multi"

    @model_validator(mode="after")
    def multilingual_model(self) -> Self:
        if self.language == "multi" and self.model != "nova-3":
            raise ValueError("Multilingual transcription requires nova-3")
        if self.language == "gu" and self.model != "nova-3":
            raise ValueError("Gujarati transcription requires nova-3")
        return self


class AzureSpeechToTextConfig(_Config):
    region: AzureRegion = "eastus"
    language: str = Field(default="en-US", pattern=r"^[a-z]{2,3}-[A-Z]{2}$")


class AzureTextToSpeechConfig(_Config):
    region: AzureRegion = "eastus"
    voice: str = Field(default="en-US-JennyNeural", pattern=_VOICE)
    hindi_voice: str = Field(default="hi-IN-SwaraNeural", pattern=_VOICE)


class ElevenLabsConfig(_Config):
    model_id: Literal["eleven_turbo_v2_5", "eleven_multilingual_v2", "eleven_v3"] = (
        "eleven_turbo_v2_5"
    )
    voice_id: str = Field(default="21m00Tcm4TlvDq8ikWAM", pattern=r"^[A-Za-z0-9]{20}$")


class RoutedVoiceConfig(ElevenLabsConfig):
    region: AzureRegion = "eastus"


class RoutedRecognitionConfig(_Config):
    region: AzureRegion = "eastus"
    gujarati_recognition: Literal["deepgram", "azure_speech"] = "deepgram"
    language: Literal["en-US", "hi-IN", "gu-IN", "es-ES", "fr-FR", "de-DE", "pt-BR"] = "en-US"


class GeminiChatConfig(_Config):
    # The platform allowlist (TENANT_LLM_MODELS) is enforced by the service.
    model: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]{1,62}$")
    # Lower levels answer sooner; None omits it for models that do not accept one.
    thinking_level: Literal["low", "medium", "high"] | None = "low"


class GeminiEmbeddingsConfig(_Config):
    # The vector index is fixed at 1536 dimensions; another size needs a rebuild.
    model: Literal["gemini-embedding-2"] = "gemini-embedding-2"


class OpenRouterChatConfig(_Config):
    # An OpenRouter slug such as google/gemini-3.8-flash; the allowlist still applies.
    model: str = Field(pattern=r"^[a-z0-9][a-z0-9.\-]{0,40}/[a-z0-9][a-z0-9.\-:]{1,80}$")
    thinking_level: Literal["minimal", "low", "medium", "high"] | None = "low"


class OpenRouterEmbeddingsConfig(_Config):
    # Produces 1536 dimensions natively, matching the vector index.
    model: Literal["openai/text-embedding-3-small"] = "openai/text-embedding-3-small"


class SmtpConfig(_Config):
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9.\-]+$")
    port: int = Field(default=587, ge=1, le=65535)
    tls_mode: Literal["starttls", "tls", "plain"] = "starttls"
    username: str = Field(default="", max_length=254, pattern=r"^[\x21-\x7e]*$")
    # The address mail is sent from.
    sender: str = Field(max_length=254, pattern=r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


class SmtpSecrets(_Secrets):
    # Empty for a server that takes mail without signing in.
    password: SecretStr = SecretStr("")


class OAuthSecrets(_Secrets):
    client_secret: SecretStr = Field(min_length=8, max_length=256)

    @model_validator(mode="after")
    def printable(self) -> Self:
        if not _printable(self.client_secret):
            raise ValueError("Invalid client secret")
        return self


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


class RoutedRecognitionSecrets(AzureSecrets):
    deepgram_api_key: SecretStr = Field(min_length=16, max_length=256)

    @model_validator(mode="after")
    def valid_deepgram_key(self) -> Self:
        if not _printable(self.deepgram_api_key):
            raise ValueError("Invalid Deepgram key")
        return self


class RoutedVoiceSecrets(AzureSecrets):
    elevenlabs_api_key: SecretStr = Field(min_length=16, max_length=256)

    @model_validator(mode="after")
    def valid_elevenlabs_key(self) -> Self:
        if not _printable(self.elevenlabs_api_key):
            raise ValueError("Invalid ElevenLabs key")
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


_SPECS = (
    ProviderSpec(
        "language_router",
        Purpose.STT,
        "Automatic: Deepgram + Azure Speech",
        RoutedRecognitionConfig,
        RoutedRecognitionSecrets,
        locked_fields=frozenset({"region"}),
        suffix_field="speech_key",
    ),
    ProviderSpec(
        "language_router",
        Purpose.TTS,
        "Automatic: ElevenLabs + Azure Speech",
        RoutedVoiceConfig,
        RoutedVoiceSecrets,
        locked_fields=frozenset({"region"}),
        suffix_field="speech_key",
    ),
    ProviderSpec(
        "twilio",
        Purpose.TELEPHONY,
        "Twilio",
        EmptyConfig,
        TwilioSecrets,
        account_pattern=r"^AC[0-9a-fA-F]{32}$",
        suffix_field="auth_token",
    ),
    ProviderSpec("deepgram", Purpose.STT, "Deepgram", DeepgramConfig, ApiKeySecrets),
    ProviderSpec(
        "azure_speech",
        Purpose.STT,
        "Azure Speech",
        AzureSpeechToTextConfig,
        AzureSecrets,
        locked_fields=frozenset({"region"}),
        suffix_field="speech_key",
    ),
    ProviderSpec("elevenlabs", Purpose.TTS, "ElevenLabs", ElevenLabsConfig, ApiKeySecrets),
    ProviderSpec(
        "azure_speech",
        Purpose.TTS,
        "Azure Speech",
        AzureTextToSpeechConfig,
        AzureSecrets,
        locked_fields=frozenset({"region"}),
        suffix_field="speech_key",
    ),
    ProviderSpec("gemini", Purpose.LLM, "Google Gemini", GeminiChatConfig, ApiKeySecrets),
    ProviderSpec(
        "gemini", Purpose.EMBEDDINGS, "Google Gemini", GeminiEmbeddingsConfig, ApiKeySecrets
    ),
    ProviderSpec("openrouter", Purpose.LLM, "OpenRouter", OpenRouterChatConfig, ApiKeySecrets),
    ProviderSpec(
        "openrouter", Purpose.EMBEDDINGS, "OpenRouter", OpenRouterEmbeddingsConfig, ApiKeySecrets
    ),
)
SMTP = ProviderSpec(
    "smtp", Purpose.EMAIL, "Email server (SMTP)", SmtpConfig, SmtpSecrets, suffix_field="password"
)
# What the platform itself connects. Knowledge search uses the AI model account, so
# embeddings have no connection of their own here.
PLATFORM_CATALOG: dict[tuple[Purpose, str], ProviderSpec] = {
    **{
        (spec.purpose, spec.provider): spec for spec in _SPECS if spec.purpose != Purpose.EMBEDDINGS
    },
    (SMTP.purpose, SMTP.provider): SMTP,
    # The apps registered with Google and Microsoft, through which providers connect
    # their own calendars. The account identifier is the app's client ID.
    (Purpose.GOOGLE_CALENDAR, "google"): ProviderSpec(
        "google",
        Purpose.GOOGLE_CALENDAR,
        "Google Calendar",
        EmptyConfig,
        OAuthSecrets,
        account_pattern=r"^[0-9A-Za-z._\-]{10,170}\.apps\.googleusercontent\.com$",
        suffix_field="client_secret",
    ),
    (Purpose.MICROSOFT_CALENDAR, "microsoft"): ProviderSpec(
        "microsoft",
        Purpose.MICROSOFT_CALENDAR,
        "Outlook calendar",
        EmptyConfig,
        OAuthSecrets,
        account_pattern=r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$",
        suffix_field="client_secret",
    ),
}
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


def platform_spec(purpose: str, provider: str) -> ProviderSpec:
    try:
        return PLATFORM_CATALOG[(Purpose(purpose), provider)]
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


def catalog_entries(
    llm_models: tuple[str, ...], *, platform: bool = False
) -> list[ProviderCatalogEntry]:
    entries: list[ProviderCatalogEntry] = []
    for spec in PLATFORM_CATALOG.values() if platform else _SPECS:
        schema = spec.config.model_json_schema()
        if spec.purpose == Purpose.LLM:
            # Each provider is offered only the allowed models written in its own form.
            pattern = re.compile(str(schema["properties"]["model"]["pattern"]))
            schema["properties"]["model"]["enum"] = [m for m in llm_models if pattern.fullmatch(m)]
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
