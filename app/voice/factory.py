"""Speech providers for one call: the clinic's own connection, or the platform account.

Nothing here chooses between the two; the caller looks up the clinic's connection
first and asks for the platform provider only when there is none.
"""

from collections.abc import Callable

from app.core.settings import Settings
from app.integrations.resolver import ResolvedIntegration
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import SpeechToTextProvider
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.base import TextToSpeechProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider

LanguageResolver = Callable[[], str]


def stt_from_connection(
    settings: Settings,
    connection: ResolvedIntegration,
    language_resolver: LanguageResolver | None = None,
) -> SpeechToTextProvider:
    config = connection.config
    if connection.provider == "deepgram":
        return DeepgramSTTProvider(
            api_key=connection.secret("api_key"),
            model=str(config["model"]),
            language=str(config["language"]),
        )
    if connection.provider == "azure_speech":
        return AzureSTTProvider(
            speech_key=connection.secret("speech_key"),
            region=str(config["region"]),
            language=str(config["language"]),
            language_resolver=language_resolver,
        )
    raise ValueError("Unsupported STT provider")


def tts_from_connection(
    settings: Settings, connection: ResolvedIntegration
) -> TextToSpeechProvider:
    config = connection.config
    if connection.provider == "elevenlabs":
        return ElevenLabsTTSProvider(
            api_key=connection.secret("api_key"),
            default_voice_id=str(config["voice_id"]),
            model_id=str(config["model_id"]),
        )
    if connection.provider == "azure_speech":
        return AzureTTSProvider(
            speech_key=connection.secret("speech_key"),
            region=str(config["region"]),
            default_voice=str(config["voice"]),
            hindi_voice=str(config["hindi_voice"]),
        )
    raise ValueError("Unsupported TTS provider")


def platform_stt(
    settings: Settings, language_resolver: LanguageResolver | None = None
) -> SpeechToTextProvider | None:
    """The platform account's speech-to-text, or None when its key is not configured."""
    if settings.stt_provider == "deepgram" and settings.deepgram_api_key.get_secret_value():
        return DeepgramSTTProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
        )
    if settings.stt_provider == "azure" and settings.azure_speech_key.get_secret_value():
        return AzureSTTProvider(
            speech_key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            language=settings.azure_speech_language,
            language_resolver=language_resolver,
        )
    return None


def platform_tts(settings: Settings) -> TextToSpeechProvider | None:
    """The platform account's text-to-speech, or None when its key is not configured."""
    if settings.tts_provider == "elevenlabs" and settings.elevenlabs_api_key.get_secret_value():
        return ElevenLabsTTSProvider(
            api_key=settings.elevenlabs_api_key,
            default_voice_id=settings.elevenlabs_voice_id,
            model_id=settings.elevenlabs_model_id,
        )
    if settings.tts_provider == "azure" and settings.azure_speech_key.get_secret_value():
        return AzureTTSProvider(
            speech_key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            default_voice=settings.azure_speech_voice,
            hindi_voice=settings.azure_hindi_voice,
        )
    return None


def follows_caller_language(settings: Settings, connection: ResolvedIntegration | None) -> bool:
    """Whether the speech-to-text in use can transcribe more than one language per call.

    Deepgram does when set to its multilingual mode; Azure does by switching locale.
    """
    if connection is not None:
        return connection.provider == "azure_speech" or connection.config.get("language") == "multi"
    return settings.stt_provider == "azure" or settings.deepgram_language == "multi"
