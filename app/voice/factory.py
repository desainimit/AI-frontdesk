"""Speech providers for one call: the clinic's own connection, or the platform account.

Nothing here chooses between the two; the caller looks up the clinic's connection
first and asks for the platform provider only when there is none.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from app.conversation.locales import LOCALES, LanguagePolicy
from app.core.settings import Settings, SpeakingSpeed
from app.integrations.resolver import IntegrationUnavailableError, ResolvedIntegration
from app.voice.routing import RoutedSTTProvider, RoutedTTSProvider
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import SpeechToTextProvider
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.base import TextToSpeechProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider

LanguageResolver = Callable[[], str]


@dataclass(frozen=True)
class SpeechProfile:
    """Non-secret metadata needed to check a selected provider's capabilities."""

    provider: str
    config: Mapping[str, Any]


class GujaratiVoiceConfigurationError(IntegrationUnavailableError):
    code = "gujarati_voice_not_ready"
    public_message = "The speech connections do not support the selected languages."

    def __init__(self, issues: list[tuple[str, str]]) -> None:
        self.issues = issues
        self.public_message = " ".join(message for _, message in issues)
        super().__init__(self.public_message)


def validate_gujarati_voice(
    settings: Settings,
    policy: LanguagePolicy,
    stt: ResolvedIntegration | SpeechProfile | None,
    tts: ResolvedIntegration | SpeechProfile | None,
) -> None:
    """Check speech capabilities, independently of the tenant's main language."""
    spoken_languages = (
        set(policy.enabled_languages) if policy.auto_detect else {policy.default_language}
    )
    if "gu" not in spoken_languages:
        return
    stt_provider = stt.provider if stt else settings.stt_provider
    tts_provider = tts.provider if tts else settings.tts_provider
    tts_model = tts.config.get("model_id") if tts else settings.elevenlabs_model_id
    issues: list[tuple[str, str]] = []
    stt_source = "workspace" if stt else "platform"
    tts_source = "workspace" if tts else "platform"
    if tts_provider not in {"azure", "azure_speech", "language_router"} and not (
        tts_provider == "elevenlabs" and tts_model == "eleven_v3"
    ):
        issues.append(
            (
                "speech_connections.tts",
                f"The {tts_source} voice connection does not support Gujarati. "
                "For ElevenLabs, select Eleven v3; or use Azure Speech or "
                "Automatic: ElevenLabs + Azure Speech in the voice connection settings.",
            )
        )
    model = stt.config.get("model") if stt else settings.deepgram_model
    language = stt.config.get("language") if stt else settings.deepgram_language
    if stt_provider not in {"azure", "azure_speech", "language_router"}:
        if len(spoken_languages) > 1:
            issues.append(
                (
                    "speech_connections.stt",
                    f"The {stt_source} recognition connection cannot automatically "
                    "switch to Gujarati. "
                    "Deepgram Nova 3 supports Gujarati with language=gu, but its multilingual mode "
                    "does not include Gujarati. Choose Automatic: Deepgram + Azure Speech "
                    "with Gujarati recognition set to Deepgram Nova 3, or Azure Speech. "
                    "Your main language can stay unchanged.",
                )
            )
        elif stt_provider != "deepgram" or model != "nova-3" or language != "gu":
            issues.append(
                (
                    "speech_connections.stt",
                    f"Set the {stt_source} recognition connection to Deepgram Nova 3 with "
                    "recognition language Gujarati, Azure Speech, or "
                    "Automatic: Deepgram + Azure Speech.",
                )
            )
    if issues:
        raise GujaratiVoiceConfigurationError(issues)


def recognition_locales(policy: LanguagePolicy | None) -> tuple[str, ...]:
    """Audio candidates are call-local and include only tenant-enabled languages."""
    if policy is None or not policy.auto_detect or len(policy.enabled_languages) < 2:
        return ()
    return tuple(LOCALES[code].speech_locale for code in policy.enabled_languages)


def _routed_stt(
    config: Mapping[str, Any],
    secrets: Mapping[str, SecretStr],
    policy: LanguagePolicy | None,
) -> RoutedSTTProvider:
    # Provider instances and locale candidates belong to this call only.
    language = policy.default_language if policy else str(config["language"]).split("-")[0]
    enabled = tuple(policy.enabled_languages) if policy else (language,)
    auto = bool(recognition_locales(policy))
    return RoutedSTTProvider(
        AzureSTTProvider(
            speech_key=secrets["speech_key"],
            region=str(config["region"]),
            language=LOCALES[language].speech_locale,
            candidate_languages=recognition_locales(policy),
        ),
        DeepgramSTTProvider(
            api_key=secrets["deepgram_api_key"],
            model="nova-3",
            language="multi" if auto else language,
        ),
        gujarati=(
            DeepgramSTTProvider(api_key=secrets["deepgram_api_key"], model="nova-3", language="gu")
            if config.get("gujarati_recognition", "deepgram") == "deepgram"
            else None
        ),
        enabled_languages=enabled,
        default_language=language,
        auto_detect=auto,
    )


def _routed_tts(
    config: Mapping[str, Any], secrets: Mapping[str, SecretStr], speed: SpeakingSpeed
) -> RoutedTTSProvider:
    return RoutedTTSProvider(
        AzureTTSProvider(
            speech_key=secrets["speech_key"], region=str(config["region"]), speed=speed
        ),
        ElevenLabsTTSProvider(
            api_key=secrets["elevenlabs_api_key"],
            speed=speed,
            default_voice_id=str(config["voice_id"]),
            model_id=str(config["model_id"]),
        ),
    )


def _hints(policy: LanguagePolicy | None) -> tuple[str, ...]:
    return tuple(policy.enabled_languages) if policy is not None else ()


def stt_from_connection(
    settings: Settings,
    connection: ResolvedIntegration,
    language_resolver: LanguageResolver | None = None,
    language_policy: LanguagePolicy | None = None,
    keyterms: tuple[str, ...] = (),
) -> SpeechToTextProvider:
    config = connection.config
    if connection.provider == "language_router":
        return _routed_stt(config, connection.secrets, language_policy)
    if connection.provider == "deepgram":
        return DeepgramSTTProvider(
            api_key=connection.secret("api_key"),
            model=str(config["model"]),
            language=str(config["language"]),
            keyterms=keyterms,
            language_hints=_hints(language_policy),
        )
    if connection.provider == "azure_speech":
        return AzureSTTProvider(
            speech_key=connection.secret("speech_key"),
            region=str(config["region"]),
            language=str(config["language"]),
            language_resolver=language_resolver,
            candidate_languages=recognition_locales(language_policy),
        )
    raise ValueError("Unsupported STT provider")


def tts_from_connection(
    settings: Settings, connection: ResolvedIntegration
) -> TextToSpeechProvider:
    config = connection.config
    if connection.provider == "language_router":
        return _routed_tts(config, connection.secrets, settings.speaking_speed)
    if connection.provider == "elevenlabs":
        return ElevenLabsTTSProvider(
            api_key=connection.secret("api_key"),
            default_voice_id=str(config["voice_id"]),
            model_id=str(config["model_id"]),
            speed=settings.speaking_speed,
        )
    if connection.provider == "azure_speech":
        return AzureTTSProvider(
            speech_key=connection.secret("speech_key"),
            region=str(config["region"]),
            default_voice=str(config["voice"]),
            hindi_voice=str(config["hindi_voice"]),
            speed=settings.speaking_speed,
        )
    raise ValueError("Unsupported TTS provider")


def platform_stt(
    settings: Settings,
    language_resolver: LanguageResolver | None = None,
    language_policy: LanguagePolicy | None = None,
    keyterms: tuple[str, ...] = (),
) -> SpeechToTextProvider | None:
    """The platform account's speech-to-text, or None when its key is not configured."""
    if settings.stt_provider == "language_router":
        if all(
            settings.routed_stt_secrets.get(name, SecretStr("")).get_secret_value()
            for name in ("speech_key", "deepgram_api_key")
        ):
            return _routed_stt(
                settings.routed_stt_config, settings.routed_stt_secrets, language_policy
            )
        return None
    if settings.stt_provider == "deepgram" and settings.deepgram_api_key.get_secret_value():
        return DeepgramSTTProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
            keyterms=keyterms,
            language_hints=_hints(language_policy),
        )
    azure_key = (
        settings.azure_stt_key
        if settings.azure_stt_key.get_secret_value()
        else settings.azure_speech_key
    )
    if settings.stt_provider == "azure" and azure_key.get_secret_value():
        return AzureSTTProvider(
            speech_key=azure_key,
            region=(
                settings.azure_stt_region
                if settings.azure_stt_key.get_secret_value()
                else settings.azure_speech_region
            ),
            language=settings.azure_speech_language,
            language_resolver=language_resolver,
            candidate_languages=recognition_locales(language_policy),
        )
    return None


def platform_tts(settings: Settings) -> TextToSpeechProvider | None:
    """The platform account's text-to-speech, or None when its key is not configured."""
    if settings.tts_provider == "language_router":
        if all(
            settings.routed_tts_secrets.get(name, SecretStr("")).get_secret_value()
            for name in ("speech_key", "elevenlabs_api_key")
        ):
            return _routed_tts(
                settings.routed_tts_config, settings.routed_tts_secrets, settings.speaking_speed
            )
        return None
    if settings.tts_provider == "elevenlabs" and settings.elevenlabs_api_key.get_secret_value():
        return ElevenLabsTTSProvider(
            api_key=settings.elevenlabs_api_key,
            default_voice_id=settings.elevenlabs_voice_id,
            model_id=settings.elevenlabs_model_id,
            speed=settings.speaking_speed,
        )
    azure_key = (
        settings.azure_tts_key
        if settings.azure_tts_key.get_secret_value()
        else settings.azure_speech_key
    )
    if settings.tts_provider == "azure" and azure_key.get_secret_value():
        return AzureTTSProvider(
            speech_key=azure_key,
            region=(
                settings.azure_tts_region
                if settings.azure_tts_key.get_secret_value()
                else settings.azure_speech_region
            ),
            default_voice=settings.azure_speech_voice,
            hindi_voice=settings.azure_hindi_voice,
            speed=settings.speaking_speed,
        )
    return None


def follows_caller_language(settings: Settings, connection: ResolvedIntegration | None) -> bool:
    """Whether the speech-to-text in use can transcribe more than one language per call.

    Deepgram does when set to its multilingual mode; Azure does by switching locale.
    """
    if connection is not None:
        return (
            connection.provider in {"azure_speech", "language_router"}
            or connection.config.get("language") == "multi"
        )
    return (
        settings.stt_provider in {"azure", "language_router"}
        or settings.deepgram_language == "multi"
    )
