"""Speech providers for one call: the clinic's own connection, or the platform account.

Nothing here chooses between the two; the caller looks up the clinic's connection
first and asks for the platform provider only when there is none.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from app.conversation.locales import LanguagePolicy
from app.core.settings import Settings, SpeakingSpeed
from app.integrations.resolver import IntegrationUnavailableError, ResolvedIntegration
from app.languages import language as registry_language
from app.voice.routing import RoutedSTTProvider, RoutedTTSProvider
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import SpeechToTextProvider
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.stt.sarvam import SarvamSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.base import TextToSpeechProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider
from app.voice.tts.sarvam import SarvamTTSProvider

LanguageResolver = Callable[[], str]


@dataclass(frozen=True)
class SpeechProfile:
    """Non-secret metadata needed to check a selected provider's capabilities."""

    provider: str
    config: Mapping[str, Any]


class VoiceLanguageError(IntegrationUnavailableError):
    code = "voice_languages_not_ready"
    public_message = "The speech connections do not support the selected languages."

    def __init__(self, issues: list[tuple[str, str]]) -> None:
        self.issues = issues
        self.public_message = " ".join(message for _, message in issues)
        super().__init__(self.public_message)


# What Deepgram Nova 3 understands in its multilingual mode.
_NOVA_MULTI = frozenset({"en", "es", "fr", "de", "hi", "pt"})


def _understands(provider: str, config: Mapping[str, Any], code: str, alone: bool) -> bool:
    entry = registry_language(code)
    if provider in {"azure", "azure_speech", "language_router"}:
        return True
    if provider == "sarvam":
        return entry.sarvam is not None
    if provider == "deepgram":
        language = str(config.get("language") or "multi")
        if language != "multi":
            # One fixed language: right only for a clinic speaking just that one, and
            # Gujarati only on Nova 3.
            model_ok = language != "gu" or str(config.get("model") or "") == "nova-3"
            return alone and model_ok and language == entry.family
        if str(config.get("model") or "") == "flux":
            return entry.flux
        return entry.family in _NOVA_MULTI
    return False


def _speaks(provider: str, config: Mapping[str, Any], code: str) -> bool:
    entry = registry_language(code)
    if provider in {"azure", "azure_speech"}:
        return True
    if provider == "language_router":
        # Gujarati goes to Azure; every other language to the ElevenLabs voice.
        return entry.family == "gu" or str(config.get("model_id")) in entry.elevenlabs_models
    if provider == "elevenlabs":
        return str(config.get("model_id")) in entry.elevenlabs_models
    if provider == "sarvam":
        return entry.sarvam is not None
    return False


def validate_voices(
    settings: Settings,
    policy: LanguagePolicy,
    stt: ResolvedIntegration | SpeechProfile | None,
    tts: ResolvedIntegration | SpeechProfile | None,
) -> None:
    """Every language the clinic offers can be understood and spoken by the speech
    connections in use, the clinic's own or the platform's. Raises VoiceLanguageError
    with one plain reason per connection that cannot."""
    spoken = sorted(
        set(policy.enabled_languages) if policy.auto_detect else {policy.default_language}
    )
    stt_provider = stt.provider if stt else settings.stt_provider
    stt_config: Mapping[str, Any] = (
        stt.config
        if stt
        else {"model": settings.deepgram_model, "language": settings.deepgram_language}
    )
    tts_provider = tts.provider if tts else settings.tts_provider
    tts_config: Mapping[str, Any] = (
        tts.config if tts else {"model_id": settings.elevenlabs_model_id}
    )
    unheard = [
        code
        for code in spoken
        if not _understands(stt_provider, stt_config, code, len(spoken) == 1)
    ]
    unspoken = [code for code in spoken if not _speaks(tts_provider, tts_config, code)]
    issues: list[tuple[str, str]] = []

    def names(codes: list[str]) -> str:
        return ", ".join(registry_language(code).name for code in codes)

    def indian(codes: list[str]) -> bool:
        return all(registry_language(code).sarvam for code in codes)

    if unheard:
        source = "clinic's own" if stt else "platform"
        better = "Sarvam or Azure Speech" if indian(unheard) else "Deepgram Flux or Azure Speech"
        issues.append(
            (
                "speech_connections.stt",
                f"The {source} speech recognition connection cannot understand "
                f"{names(unheard)}. Choose {better} for speech recognition, or stop "
                "offering that language.",
            )
        )
    if unspoken:
        source = "clinic's own" if tts else "platform"
        better = (
            "Sarvam, Azure Speech or ElevenLabs v3"
            if indian(unspoken)
            else "Azure Speech or ElevenLabs"
        )
        issues.append(
            (
                "speech_connections.tts",
                f"The {source} voice connection cannot speak {names(unspoken)}. "
                f"Choose {better} for the voice, or stop offering that language.",
            )
        )
    if issues:
        raise VoiceLanguageError(issues)


def recognition_locales(policy: LanguagePolicy | None) -> tuple[str, ...]:
    """Audio candidates are call-local and include only tenant-enabled languages."""
    if policy is None or not policy.auto_detect or len(policy.enabled_languages) < 2:
        return ()
    return tuple(registry_language(code).default_locale for code in policy.enabled_languages)


def _routed_stt(
    config: Mapping[str, Any],
    secrets: Mapping[str, SecretStr],
    policy: LanguagePolicy | None,
    keyterms: tuple[str, ...] = (),
) -> RoutedSTTProvider:
    # Provider instances and locale candidates belong to this call only.
    language = policy.default_language if policy else str(config["language"]).split("-")[0]
    enabled = tuple(policy.enabled_languages) if policy else (language,)
    auto = bool(recognition_locales(policy))
    return RoutedSTTProvider(
        AzureSTTProvider(
            speech_key=secrets["speech_key"],
            region=str(config["region"]),
            language=registry_language(language).default_locale,
            candidate_languages=recognition_locales(policy),
        ),
        DeepgramSTTProvider(
            api_key=secrets["deepgram_api_key"],
            model="nova-3",
            language="multi" if auto else language,
            # The names a caller is likely to say, as on a single-provider call.
            keyterms=keyterms,
        ),
        gujarati=(
            DeepgramSTTProvider(
                api_key=secrets["deepgram_api_key"],
                model="nova-3",
                language="gu",
                keyterms=keyterms,
            )
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


def _sarvam_language(policy: LanguagePolicy | None) -> str | None:
    """The one language Sarvam listens for, or None to let it tell which Indian language
    is spoken (a clinic offering several)."""
    if policy is None:
        return None
    spoken = set(policy.enabled_languages) if policy.auto_detect else {policy.default_language}
    return policy.default_language if len(spoken) == 1 else None


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
        return _routed_stt(config, connection.secrets, language_policy, keyterms)
    if connection.provider == "deepgram":
        return DeepgramSTTProvider(
            api_key=connection.secret("api_key"),
            model=str(config["model"]),
            language=str(config["language"]),
            keyterms=keyterms,
            language_hints=_hints(language_policy),
        )
    if connection.provider == "sarvam":
        return SarvamSTTProvider(
            connection.secret("api_key"),
            str(config["model"]),
            _sarvam_language(language_policy),
            stream_type=str(config["stream_type"]),
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
    if connection.provider == "sarvam":
        return SarvamTTSProvider(
            connection.secret("api_key"),
            model=str(config["model"]),
            speaker=str(config["speaker"]),
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
                settings.routed_stt_config, settings.routed_stt_secrets, language_policy, keyterms
            )
        return None
    if settings.stt_provider == "sarvam" and settings.sarvam_stt_key.get_secret_value():
        return SarvamSTTProvider(
            settings.sarvam_stt_key,
            settings.sarvam_stt_model,
            _sarvam_language(language_policy),
            stream_type=settings.sarvam_stream_type,
        )
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
    if settings.tts_provider == "sarvam" and settings.sarvam_tts_key.get_secret_value():
        return SarvamTTSProvider(
            settings.sarvam_tts_key,
            model=settings.sarvam_tts_model,
            speaker=settings.sarvam_speaker,
            speed=settings.speaking_speed,
        )
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
