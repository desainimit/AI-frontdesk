from collections.abc import Callable

from app.config.settings import Settings
from app.integrations.resolver import ResolvedIntegration
from app.observability.tracing import Telemetry
from app.voice.circuit import CircuitBreaker, CircuitRegistry
from app.voice.metrics import VoiceMetrics
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import SpeechToTextProvider
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.stt.fallback import FallbackSTTProvider
from app.voice.stt.mock import MockSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.base import TextToSpeechProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider
from app.voice.tts.fallback import FallbackTTSProvider
from app.voice.tts.mock import MockTTSProvider


def build_stt_provider(
    settings: Settings,
    telemetry: Telemetry | None = None,
    *,
    language_resolver: Callable[[], str] | None = None,
) -> SpeechToTextProvider:
    """Builds configured STT provider with automatic fallback and circuit breaker."""
    primary_name = settings.primary_stt_provider.lower()
    fallback_name = settings.fallback_stt_provider.lower()

    if primary_name == "deepgram":
        primary: SpeechToTextProvider = DeepgramSTTProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
            base_url=settings.deepgram_base_url,
            timeout_seconds=settings.stt_timeout_seconds,
        )
    elif primary_name == "azure":
        primary = AzureSTTProvider(
            speech_key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            language=settings.azure_speech_language,
            language_resolver=language_resolver,
            timeout_seconds=settings.stt_timeout_seconds,
        )
    elif primary_name == "mock":
        primary = MockSTTProvider(name=primary_name)
    else:
        raise ValueError("Unsupported STT provider")

    if fallback_name == "azure":
        fallback: SpeechToTextProvider = AzureSTTProvider(
            speech_key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            language=settings.azure_speech_language,
            language_resolver=language_resolver,
            timeout_seconds=settings.stt_timeout_seconds,
        )
    elif fallback_name == "deepgram":
        fallback = DeepgramSTTProvider(
            api_key=settings.deepgram_api_key,
            model=settings.deepgram_model,
            language=settings.deepgram_language,
            base_url=settings.deepgram_base_url,
            timeout_seconds=settings.stt_timeout_seconds,
        )
    elif fallback_name in {"mock", "none", ""}:
        fallback = MockSTTProvider(name=fallback_name)
    else:
        raise ValueError("Unsupported STT fallback")

    if fallback_name in ("none", "") or primary_name == fallback_name:
        return primary

    circuit = CircuitBreaker(
        name=f"stt_{primary_name}",
        failure_threshold=settings.voice_circuit_breaker_failures,
        reset_seconds=settings.voice_circuit_breaker_reset_seconds,
    )
    return FallbackSTTProvider(
        primary=primary,
        fallback=fallback,
        circuit_breaker=circuit,
        metrics=VoiceMetrics(telemetry),
    )


def build_tts_provider(
    settings: Settings, telemetry: Telemetry | None = None
) -> TextToSpeechProvider:
    """Builds configured TTS provider with automatic fallback and circuit breaker."""
    primary_name = settings.primary_tts_provider.lower()
    fallback_name = settings.fallback_tts_provider.lower()

    if primary_name == "elevenlabs":
        primary: TextToSpeechProvider = ElevenLabsTTSProvider(
            api_key=settings.elevenlabs_api_key,
            default_voice_id=settings.elevenlabs_voice_id,
            model_id=settings.elevenlabs_model_id,
            output_format=settings.elevenlabs_output_format,
            base_url=settings.elevenlabs_base_url,
            timeout_seconds=settings.tts_timeout_seconds,
        )
    elif primary_name == "azure":
        primary = AzureTTSProvider(
            speech_key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            default_voice=settings.azure_speech_voice,
            hindi_voice=settings.azure_hindi_voice,
            output_format=settings.azure_speech_output_format,
            timeout_seconds=settings.tts_timeout_seconds,
        )
    elif primary_name == "mock":
        primary = MockTTSProvider(name=primary_name)
    else:
        raise ValueError("Unsupported TTS provider")

    if fallback_name == "azure":
        fallback: TextToSpeechProvider = AzureTTSProvider(
            speech_key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            default_voice=settings.azure_speech_voice,
            hindi_voice=settings.azure_hindi_voice,
            output_format=settings.azure_speech_output_format,
            timeout_seconds=settings.tts_timeout_seconds,
        )
    elif fallback_name == "elevenlabs":
        fallback = ElevenLabsTTSProvider(
            api_key=settings.elevenlabs_api_key,
            default_voice_id=settings.elevenlabs_voice_id,
            model_id=settings.elevenlabs_model_id,
            output_format=settings.elevenlabs_output_format,
            base_url=settings.elevenlabs_base_url,
            timeout_seconds=settings.tts_timeout_seconds,
        )
    elif fallback_name in {"mock", "none", ""}:
        fallback = MockTTSProvider(name=fallback_name)
    else:
        raise ValueError("Unsupported TTS fallback")

    if fallback_name in ("none", "") or primary_name == fallback_name:
        return primary

    circuit = CircuitBreaker(
        name=f"tts_{primary_name}",
        failure_threshold=settings.voice_circuit_breaker_failures,
        reset_seconds=settings.voice_circuit_breaker_reset_seconds,
    )
    return FallbackTTSProvider(
        primary=primary,
        fallback=fallback,
        circuit_breaker=circuit,
        metrics=VoiceMetrics(telemetry),
    )


def _tenant_stt(
    settings: Settings,
    connection: ResolvedIntegration,
    language_resolver: Callable[[], str] | None,
) -> SpeechToTextProvider:
    config = connection.config
    if connection.provider == "deepgram":
        return DeepgramSTTProvider(
            api_key=connection.secret("api_key"),
            model=str(config["model"]),
            language=str(config["language"]),
            base_url=settings.deepgram_base_url,
            timeout_seconds=settings.stt_timeout_seconds,
        )
    if connection.provider == "azure_speech":
        return AzureSTTProvider(
            speech_key=connection.secret("speech_key"),
            region=str(config["region"]),
            language=str(config["language"]),
            language_resolver=language_resolver,
            timeout_seconds=settings.stt_timeout_seconds,
        )
    raise ValueError("Unsupported STT provider")


def _tenant_tts(settings: Settings, connection: ResolvedIntegration) -> TextToSpeechProvider:
    config = connection.config
    if connection.provider == "elevenlabs":
        return ElevenLabsTTSProvider(
            api_key=connection.secret("api_key"),
            default_voice_id=str(config["voice_id"]),
            model_id=str(config["model_id"]),
            output_format=settings.elevenlabs_output_format,
            base_url=settings.elevenlabs_base_url,
            timeout_seconds=settings.tts_timeout_seconds,
        )
    if connection.provider == "azure_speech":
        return AzureTTSProvider(
            speech_key=connection.secret("speech_key"),
            region=str(config["region"]),
            default_voice=str(config["voice"]),
            hindi_voice=str(config["hindi_voice"]),
            output_format=settings.azure_speech_output_format,
            timeout_seconds=settings.tts_timeout_seconds,
        )
    raise ValueError("Unsupported TTS provider")


def build_tenant_stt_provider(
    settings: Settings,
    telemetry: Telemetry | None,
    circuits: CircuitRegistry,
    primary: ResolvedIntegration,
    fallback: ResolvedIntegration | None = None,
    *,
    language_resolver: Callable[[], str] | None = None,
) -> SpeechToTextProvider:
    """STT from one tenant's own connections; never from platform provider keys."""
    provider = _tenant_stt(settings, primary, language_resolver)
    if fallback is None:
        return provider
    return FallbackSTTProvider(
        primary=provider,
        fallback=_tenant_stt(settings, fallback, language_resolver),
        circuit_breaker=circuits.get(
            f"stt:{primary.scope}",
            settings.voice_circuit_breaker_failures,
            settings.voice_circuit_breaker_reset_seconds,
        ),
        metrics=VoiceMetrics(telemetry),
    )


def build_tenant_tts_provider(
    settings: Settings,
    telemetry: Telemetry | None,
    circuits: CircuitRegistry,
    primary: ResolvedIntegration,
    fallback: ResolvedIntegration | None = None,
) -> TextToSpeechProvider:
    """TTS from one tenant's own connections; never from platform provider keys."""
    provider = _tenant_tts(settings, primary)
    if fallback is None:
        return provider
    return FallbackTTSProvider(
        primary=provider,
        fallback=_tenant_tts(settings, fallback),
        circuit_breaker=circuits.get(
            f"tts:{primary.scope}",
            settings.voice_circuit_breaker_failures,
            settings.voice_circuit_breaker_reset_seconds,
        ),
        metrics=VoiceMetrics(telemetry),
    )
