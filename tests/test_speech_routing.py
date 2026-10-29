"""Language routing contracts, including provider changes within one call."""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.conversation.locales import LanguagePolicy
from app.core.settings import Settings
from app.integrations.catalog import (
    InvalidIntegrationError,
    Purpose,
    spec_for,
    validate_config,
    validate_secrets,
)
from app.integrations.resolver import ResolvedIntegration
from app.integrations.verifiers import HttpProviderVerifier
from app.platform.connections import platform_values
from app.platform.models import PlatformConnection
from app.tenants.readiness import _stt_key, _tts_key  # pyright: ignore[reportPrivateUsage]
from app.voice.factory import (
    VoiceLanguageError,
    follows_caller_language,
    platform_stt,
    platform_tts,
    stt_from_connection,
    tts_from_connection,
    validate_voices,
)
from app.voice.routing import RoutedSTTProvider, RoutedTTSProvider
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import STTTimeoutError, STTTransientError, TranscriptionResult
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider


def policy(*languages: str, default: str = "en", auto: bool = True) -> LanguagePolicy:
    return LanguagePolicy(
        default_language=default, enabled_languages=list(languages), auto_detect=auto
    )


def connection(purpose: str, **config: Any) -> ResolvedIntegration:
    spec = spec_for(purpose, "language_router")
    secret_name = "deepgram_api_key" if purpose == "stt" else "elevenlabs_api_key"
    return ResolvedIntegration(
        uuid4(),
        uuid4(),
        uuid4(),
        "language_router",
        purpose,
        "primary",
        None,
        validate_config(spec, config),
        {"speech_key": SecretStr("azure-key" * 4), secret_name: SecretStr("other-key" * 4)},
    )


@pytest.mark.parametrize("other", ["en", "hi", "es", "fr", "de", "pt"])
@pytest.mark.parametrize("gujarati_recognition", ["deepgram", "azure_speech"])
async def test_same_call_routes_into_gujarati_and_back_from_audio(
    settings: Settings,
    other: str,
    gujarati_recognition: str,
) -> None:
    requests: list[httpx.Request] = []
    detections = iter(
        [("gu-IN", "Gujarati first"), (f"{other}-XX", "Other turn"), ("gu-IN", "Gujarati again")]
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.deepgram.com":
            return httpx.Response(
                200,
                json={
                    "results": {
                        "channels": [
                            {
                                "alternatives": [
                                    {
                                        "transcript": "Deepgram transcript",
                                        "confidence": 0.9,
                                        "languages": ["en"],
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
        locale, text = next(detections)
        from app.languages import language as registry_language

        locale = "gu-IN" if locale == "gu-IN" else registry_language(other).default_locale
        return httpx.Response(
            200, json={"phrases": [{"text": text, "locale": locale, "confidence": 0.97}]}
        )

    routed = stt_from_connection(
        settings,
        connection("stt", gujarati_recognition=gujarati_recognition),
        language_policy=policy(other, "gu", default=other),
    )
    assert isinstance(routed, RoutedSTTProvider)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        routed.azure._client = client  # type: ignore[attr-defined]
        routed.deepgram._client = client  # type: ignore[attr-defined]
        routed.gujarati._client = client  # type: ignore[attr-defined]
        first = await routed.transcribe(b"\0\0")
        second = await routed.transcribe(b"\0\0")
        third = await routed.transcribe(b"\0\0")
    assert first.language == third.language == "gu"
    assert second.language == other and second.languages == (other,)
    assert second.text == "Deepgram transcript"
    deepgram_requests = [request for request in requests if request.url.host == "api.deepgram.com"]
    assert [request.url.params["language"] for request in deepgram_requests] == (
        ["gu", "multi", "gu"] if gujarati_recognition == "deepgram" else ["multi"]
    )
    assert all(request.url.params["model"] == "nova-3" for request in deepgram_requests)
    assert len(requests) == (6 if gujarati_recognition == "deepgram" else 4)
    assert first.text == (
        "Deepgram transcript" if gujarati_recognition == "deepgram" else "Gujarati first"
    )
    assert second.duration_ms >= 0


@pytest.mark.parametrize("language", ["gu", "en", "hi"])
@pytest.mark.parametrize("gujarati_recognition", ["deepgram", "azure_speech"])
async def test_fixed_language_uses_one_service_only(
    settings: Settings, language: str, gujarati_recognition: str
) -> None:
    routed = stt_from_connection(
        settings,
        connection("stt", gujarati_recognition=gujarati_recognition),
        language_policy=policy(language, default=language, auto=False),
    )
    assert isinstance(routed, RoutedSTTProvider)
    from unittest.mock import AsyncMock

    routed.azure.transcribe = AsyncMock(return_value=TranscriptionResult("Azure", True))
    routed.deepgram.transcribe = AsyncMock(return_value=TranscriptionResult("Deepgram", True))
    if routed.gujarati is not routed.azure:
        routed.gujarati.transcribe = AsyncMock(
            return_value=TranscriptionResult("Deepgram Gujarati", True)
        )
    result = await routed.transcribe(b"\0\0")
    assert result.language == language
    assert result.text == (
        ("Azure" if gujarati_recognition == "azure_speech" else "Deepgram Gujarati")
        if language == "gu"
        else "Deepgram"
    )
    assert routed.azure.transcribe.await_count == (
        1 if language == "gu" and gujarati_recognition == "azure_speech" else 0
    )
    assert routed.deepgram.transcribe.await_count == (0 if language == "gu" else 1)


async def test_missing_disabled_or_failed_detection_never_guesses_from_english_main() -> None:
    from unittest.mock import AsyncMock

    for detected in (None, "ja"):
        azure = AsyncMock()
        azure.transcribe.return_value = TranscriptionResult("Untrusted", True, language=detected)
        deepgram = AsyncMock()
        routed = RoutedSTTProvider(
            azure, deepgram, enabled_languages=("en", "gu"), default_language="en", auto_detect=True
        )
        with pytest.raises(STTTransientError):
            await routed.transcribe(b"\0\0")
        deepgram.transcribe.assert_not_awaited()
    azure.transcribe.side_effect = STTTimeoutError("Unavailable")
    with pytest.raises(STTTimeoutError):
        await routed.transcribe(b"\0\0")
    deepgram.transcribe.assert_not_awaited()


async def test_silence_does_not_reach_deepgram() -> None:
    from unittest.mock import AsyncMock

    azure, deepgram = AsyncMock(), AsyncMock()
    azure.transcribe.return_value = TranscriptionResult("", True)
    routed = RoutedSTTProvider(
        azure, deepgram, enabled_languages=("en", "gu"), default_language="en", auto_detect=True
    )
    assert (await routed.transcribe(b"\0\0")).language is None
    deepgram.transcribe.assert_not_awaited()


async def test_low_detection_confidence_is_not_inflated_by_deepgram() -> None:
    from unittest.mock import AsyncMock

    azure, deepgram = AsyncMock(), AsyncMock()
    azure.transcribe.return_value = TranscriptionResult(
        "Uncertain", True, confidence=0.2, language="en"
    )
    deepgram.transcribe.return_value = TranscriptionResult(
        "Clear transcript", True, confidence=0.99
    )
    routed = RoutedSTTProvider(
        azure, deepgram, enabled_languages=("en", "gu"), default_language="en", auto_detect=True
    )
    assert (await routed.transcribe(b"\0\0")).confidence == 0.2


async def test_one_open_audio_stream_can_switch_providers_between_utterances() -> None:
    import asyncio
    from unittest.mock import AsyncMock

    azure, deepgram = AsyncMock(), AsyncMock()
    azure.transcribe.side_effect = [
        TranscriptionResult("Gujarati", True, language="gu"),
        TranscriptionResult("English", True, language="en"),
    ]
    deepgram.transcribe.return_value = TranscriptionResult("Deepgram English", True)
    routed = RoutedSTTProvider(
        azure, deepgram, enabled_languages=("en", "gu"), default_language="en", auto_detect=True
    )
    stream = await routed.create_stream()
    results = stream.results()
    # One 20ms voiced PCM frame followed by 600ms of silence ends each turn.
    audio = b"\xe8\x03" * 160 + b"\0\0" * (160 * 30)
    try:
        await stream.send_audio(audio)
        first = await asyncio.wait_for(anext(results), 1)
        await stream.send_audio(audio)
        second = await asyncio.wait_for(anext(results), 1)
        assert first.language == "gu" and first.text == "Gujarati"
        assert second.language == "en" and second.text == "Deepgram English"
        assert azure.transcribe.await_count == 2 and deepgram.transcribe.await_count == 1
    finally:
        await stream.close()
        await anext(results, None)


async def test_voice_changes_with_reply_language_and_never_passes_eleven_voice_to_azure(
    settings: Settings,
) -> None:
    routed = tts_from_connection(settings, connection("tts"))
    assert isinstance(routed, RoutedTTSProvider)
    calls: list[tuple[str, str | None, str | None]] = []

    class Voice:
        def __init__(self, name: str) -> None:
            self.name = name

        async def synthesize(
            self, text: str, voice_id: str | None = None, *, language: str | None = None
        ) -> AsyncIterator[bytes]:
            calls.append((self.name, voice_id, language))
            yield b"\0\0"

        async def health(self) -> bool:
            return True

    routed.azure, routed.elevenlabs = Voice("azure"), Voice("eleven")
    for language in ("en", "gu", "hi", "gu-IN", "fr", "en"):
        assert [
            chunk async for chunk in routed.synthesize("reply", "eleven-voice", language=language)
        ]
    assert [name for name, _, _ in calls] == [
        "eleven",
        "azure",
        "eleven",
        "azure",
        "eleven",
        "eleven",
    ]
    assert calls[1][1] is calls[3][1] is None
    assert calls[0][1] == "eleven-voice"


def test_platform_profiles_remain_separate_and_tenant_profiles_do_not_mutate_them(
    settings: Settings,
) -> None:
    stt, tts = connection("stt", region="centralindia"), connection("tts", region="eastus")
    rows = [
        (
            PlatformConnection(
                purpose=item.purpose, provider=item.provider, config=dict(item.config)
            ),
            {key: secret.get_secret_value() for key, secret in item.secrets.items()},
        )
        for item in (stt, tts)
    ]
    configured = settings.model_copy(update=platform_values(rows, None))
    before = configured.model_dump()
    recognition = platform_stt(configured, language_policy=policy("en", "gu"))
    voice = platform_tts(configured)
    assert isinstance(recognition, RoutedSTTProvider) and isinstance(voice, RoutedTTSProvider)
    assert recognition.azure.region == "centralindia"  # type: ignore[attr-defined]
    assert voice.azure.region == "eastus"  # type: ignore[attr-defined]
    validate_voices(configured, policy("en", "gu"), None, None)
    assert follows_caller_language(configured, None)
    assert _stt_key(configured) and _tts_key(configured)
    other = stt_from_connection(
        configured,
        connection("stt", region="westeurope"),
        language_policy=policy("fr", "en", default="fr"),
    )
    assert isinstance(other, RoutedSTTProvider)
    assert other.enabled_languages == ("fr", "en")
    assert recognition.enabled_languages == ("en", "gu")
    assert configured.model_dump() == before
    assert isinstance(recognition.deepgram, DeepgramSTTProvider)
    assert isinstance(voice.azure, AzureTTSProvider) and isinstance(
        voice.elevenlabs, ElevenLabsTTSProvider
    )
    assert platform_stt(configured.model_copy(update={"routed_stt_secrets": {}})) is None
    assert platform_tts(configured.model_copy(update={"routed_tts_secrets": {}})) is None


@pytest.mark.parametrize("missing", ["speech_key", "deepgram_api_key"])
def test_readiness_requires_both_routed_recognition_keys(settings: Settings, missing: str) -> None:
    keys = dict(connection("stt").secrets)
    keys.pop(missing)
    configured = settings.model_copy(
        update={
            "stt_provider": "language_router",
            "routed_stt_secrets": keys,
            "azure_speech_key": SecretStr("unrelated-legacy-key"),
        }
    )
    assert not _stt_key(configured)


@pytest.mark.parametrize("missing", ["speech_key", "elevenlabs_api_key"])
def test_readiness_requires_both_routed_voice_keys(settings: Settings, missing: str) -> None:
    keys = dict(connection("tts").secrets)
    keys.pop(missing)
    configured = settings.model_copy(
        update={
            "tts_provider": "language_router",
            "routed_tts_secrets": keys,
            "azure_speech_key": SecretStr("unrelated-legacy-key"),
        }
    )
    assert not _tts_key(configured)


def test_reports_both_speech_problems_without_blame_on_main_language(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "stt_provider": "deepgram",
            "deepgram_model": "nova-3",
            "deepgram_language": "multi",
            "tts_provider": "elevenlabs",
            "elevenlabs_model_id": "eleven_turbo_v2_5",
        }
    )
    with pytest.raises(VoiceLanguageError) as error:
        validate_voices(configured, policy("en", "gu"), None, None)
    issues = dict(error.value.issues)
    assert set(issues) == {"speech_connections.stt", "speech_connections.tts"}
    assert "platform speech recognition" in issues["speech_connections.stt"]
    assert "cannot understand Gujarati" in issues["speech_connections.stt"]
    assert "ElevenLabs v3" in issues["speech_connections.tts"]
    # A compatible workspace connection replaces only its own purpose.
    with pytest.raises(VoiceLanguageError) as error:
        validate_voices(configured, policy("en", "gu"), connection("stt"), None)
    assert [field for field, _ in error.value.issues] == ["speech_connections.tts"]


@pytest.mark.parametrize("reverse", [False, True])
def test_separate_azure_accounts_survive_row_order_and_removal(
    settings: Settings,
    reverse: bool,
) -> None:
    rows = [
        (
            PlatformConnection(
                purpose="stt",
                provider="azure_speech",
                config={
                    "language": "en-US",
                    "region": "centralindia",
                },
            ),
            {"speech_key": "recognition-key"},
        ),
        (
            PlatformConnection(
                purpose="tts",
                provider="azure_speech",
                config={
                    "voice": "en-US-JennyNeural",
                    "hindi_voice": "hi-IN-SwaraNeural",
                    "region": "eastus",
                },
            ),
            {"speech_key": "voice-key"},
        ),
    ]
    configured = settings.model_copy(
        update=platform_values(list(reversed(rows)) if reverse else rows, None)
    )
    stt, tts = platform_stt(configured), platform_tts(configured)
    assert isinstance(stt, AzureSTTProvider) and isinstance(tts, AzureTTSProvider)
    assert stt.region == "centralindia" and tts.region == "eastus"
    assert stt._speech_key.get_secret_value() == "recognition-key"  # pyright: ignore[reportPrivateUsage]
    assert tts._speech_key.get_secret_value() == "voice-key"  # pyright: ignore[reportPrivateUsage]
    assert _stt_key(configured) and _tts_key(configured)
    removed = configured.model_copy(update=platform_values([rows[1]], None))
    assert platform_stt(removed) is None and not _stt_key(removed)
    assert platform_tts(removed) is not None and _tts_key(removed)


@pytest.mark.parametrize("purpose", [Purpose.STT, Purpose.TTS])
@pytest.mark.parametrize("failed_host", [None, "azure", "other"])
async def test_verification_requires_both_accounts(
    purpose: Purpose, failed_host: str | None
) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        azure = request.url.host.endswith("microsoft.com")
        rejected = failed_host == ("azure" if azure else "other")
        return httpx.Response(403 if rejected else 200)

    saved = connection(purpose)
    verifier = HttpProviderVerifier(transport=httpx.MockTransport(respond))
    result = await verifier.verify(
        purpose,
        saved.provider,
        None,
        saved.config,
        {name: value.get_secret_value() for name, value in saved.secrets.items()},
    )
    assert result.outcome == ("verified" if failed_host is None else "invalid")
    assert len(calls) == (1 if failed_host == "azure" else 2)


@pytest.mark.parametrize(
    "purpose,key", [("stt", "deepgram_api_key"), ("tts", "elevenlabs_api_key")]
)
def test_both_keys_are_required_and_bad_secrets_are_not_echoed(purpose: str, key: str) -> None:
    spec = spec_for(purpose, "language_router")
    with pytest.raises(InvalidIntegrationError) as error:
        validate_secrets(spec, {"speech_key": "a" * 32, key: "secret\nvalue" * 3})
    assert "secret\nvalue" not in str(error.value)
    with pytest.raises(InvalidIntegrationError):
        validate_secrets(spec, {"speech_key": "a" * 32})
