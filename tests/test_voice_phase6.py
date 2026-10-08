import asyncio
import audioop
import base64
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from starlette.types import Message
from starlette.websockets import WebSocket

from app.api.telephony import run_media
from app.calls.service import CallContext
from app.config.settings import Settings
from app.voice.circuit import CircuitBreaker, CircuitState
from app.voice.factory import build_stt_provider, build_tts_provider
from app.voice.metrics import VoiceMetrics
from app.voice.pipeline import VoicePipeline
from app.voice.processors.interruption import InterruptionConfig, InterruptionHandler
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import (
    STTPermanentError,
    STTRateLimitError,
    STTTimeoutError,
    STTTransientError,
    TranscriptionResult,
)
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.stt.fallback import FallbackSTTProvider
from app.voice.stt.mock import MockSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.base import (
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider
from app.voice.tts.fallback import FallbackTTSProvider
from app.voice.tts.mock import MockTTSProvider
from app.voice.twilio import stream_token
from tests.test_telephony import voice_settings


# ---------------------------------------------------------------------------
# 1. Circuit Breaker Tests
# ---------------------------------------------------------------------------
def test_circuit_breaker_lifecycle() -> None:
    cb = CircuitBreaker(name="test", failure_threshold=3, reset_seconds=0.1)
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request()

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED

    # 3rd failure trips the breaker to OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.allow_request()

    # Before reset time, remains OPEN
    assert cb.state == CircuitState.OPEN

    # After reset_seconds, transitions to HALF_OPEN
    time.sleep(0.12)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allow_request()
    assert not cb.allow_request()  # Only one recovery probe may run at a time.

    # Success in HALF_OPEN resets to CLOSED
    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request()

    # Another failure sequence trips it again
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN

    time.sleep(0.12)
    assert cb.state == CircuitState.HALF_OPEN
    # Failure in HALF_OPEN immediately trips back to OPEN
    cb.record_failure()
    assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# 2. STT Provider Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mock_stt_provider() -> None:
    expected = TranscriptionResult(text="hello frontdesk", is_final=True)
    provider = MockSTTProvider(results=[expected])
    stream = await provider.create_stream()
    await stream.send_audio(bytes(320))
    await stream.close()

    results: list[TranscriptionResult] = []
    async for res in stream.results():
        results.append(res)

    assert len(results) == 1
    assert results[0].text == "hello frontdesk"
    assert results[0].is_final is True
    assert await provider.health() is True


@pytest.mark.asyncio
async def test_deepgram_stt_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if "bad-key" in auth:
            return httpx.Response(401, json={"error": "invalid auth"})
        if "rate-limit" in auth:
            return httpx.Response(429, json={"error": "rate limited"})
        if "server-error" in auth:
            return httpx.Response(503, json={"error": "service unavailable"})

        return httpx.Response(
            200,
            json={
                "results": {
                    "channels": [
                        {
                            "alternatives": [
                                {
                                    "transcript": "book an appointment please",
                                    "confidence": 0.98,
                                }
                            ]
                        }
                    ]
                }
            },
        )

    # Success
    good_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    good_provider = DeepgramSTTProvider(api_key=SecretStr("valid-token"), client=good_client)
    items = [await good_provider.transcribe(bytes(320))]
    assert len(items) == 1
    assert items[0].text == "book an appointment please"
    assert items[0].is_final is True

    # 401 Permanent
    bad_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bad_auth = DeepgramSTTProvider(api_key=SecretStr("bad-key"), client=bad_client)
    with pytest.raises(STTPermanentError):
        await bad_auth.transcribe(bytes(320))

    # 429 Rate limited
    rl_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rate_lim = DeepgramSTTProvider(api_key=SecretStr("rate-limit"), client=rl_client)
    with pytest.raises(STTRateLimitError):
        await rate_lim.transcribe(bytes(320))

    # 503 Transient
    srv_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    srv_err = DeepgramSTTProvider(api_key=SecretStr("server-error"), client=srv_client)
    with pytest.raises(STTTransientError):
        await srv_err.transcribe(bytes(320))


@pytest.mark.asyncio
async def test_azure_stt_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Ocp-Apim-Subscription-Key", "")
        if "bad-key" in key:
            return httpx.Response(401, json={"error": "invalid key"})
        if "timeout" in key:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(
            200,
            json={
                "RecognitionStatus": "Success",
                "DisplayText": "I need to reschedule.",
                "Offset": 100000,
                "Duration": 250000,
            },
        )

    good_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    good = AzureSTTProvider(speech_key=SecretStr("good-key"), client=good_client)
    results = [await good.transcribe(bytes(320))]
    assert len(results) == 1
    assert results[0].text == "I need to reschedule."

    bad_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bad = AzureSTTProvider(speech_key=SecretStr("bad-key"), client=bad_client)
    with pytest.raises(STTPermanentError):
        await bad.transcribe(bytes(320))

    to_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    to = AzureSTTProvider(speech_key=SecretStr("timeout"), client=to_client)
    with pytest.raises(STTTimeoutError):
        await to.transcribe(bytes(320))


@pytest.mark.asyncio
async def test_fallback_stt_provider() -> None:
    primary = MockSTTProvider(results=[TranscriptionResult(text="primary text", is_final=True)])
    fallback = MockSTTProvider(results=[TranscriptionResult(text="fallback text", is_final=True)])

    metrics = VoiceMetrics()
    cb = CircuitBreaker(name="test_stt", reset_seconds=0.1)
    fb_provider = FallbackSTTProvider(
        primary=primary,
        fallback=fallback,
        circuit_breaker=cb,
        metrics=metrics,
    )

    # 1. Primary healthy: works normally
    stream = await fb_provider.create_stream()
    await stream.send_audio(bytes(320))
    await stream.close()
    res = [r async for r in stream.results()]
    assert len(res) == 1
    assert res[0].text == "primary text"
    assert fb_provider.circuit.state == CircuitState.CLOSED

    # 2. Make primary fail on create_stream: automatic fallback to secondary
    class FailingSTT:
        name = "failing_primary"

        async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> Any:
            raise STTTransientError("Primary STT down")

        async def health(self) -> bool:
            return False

    fb_provider_failing = FallbackSTTProvider(
        primary=FailingSTT(),  # pyright: ignore[reportArgumentType]
        fallback=fallback,
        circuit_breaker=CircuitBreaker(name="failing_stt", reset_seconds=0.1),
        metrics=metrics,
    )

    s2 = await fb_provider_failing.create_stream()
    await s2.send_audio(bytes(320))
    await s2.close()
    res2 = [r async for r in s2.results()]
    assert len(res2) == 1
    assert res2[0].text == "fallback text"


# ---------------------------------------------------------------------------
# 3. TTS Provider Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_mock_tts_provider() -> None:
    tts = MockTTSProvider(total_chunks=3)
    chunks = [chunk async for chunk in tts.synthesize("test sentence")]
    assert len(chunks) == 3
    assert all(len(c) == 320 for c in chunks)
    assert await tts.health() is True


@pytest.mark.asyncio
async def test_elevenlabs_tts_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("xi-api-key", "")
        if "bad-key" in auth:
            return httpx.Response(401, json={"detail": "unauthorized"})
        if "rate-limit" in auth:
            return httpx.Response(429, json={"detail": "too many requests"})
        return httpx.Response(200, content=b"pcm16-audio-stream-chunk-1234")

    good_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    good = ElevenLabsTTSProvider(api_key=SecretStr("valid-key"), client=good_client)
    chunks = [c async for c in good.synthesize("hello")]
    assert len(chunks) == 1
    assert chunks[0] == b"pcm16-audio-stream-chunk-1234"

    bad_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bad = ElevenLabsTTSProvider(api_key=SecretStr("bad-key"), client=bad_client)
    with pytest.raises(TTSPermanentError):
        _ = [c async for c in bad.synthesize("hello")]

    rl_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rl = ElevenLabsTTSProvider(api_key=SecretStr("rate-limit"), client=rl_client)
    with pytest.raises(TTSRateLimitError):
        _ = [c async for c in rl.synthesize("hello")]


@pytest.mark.asyncio
async def test_azure_tts_success_and_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Ocp-Apim-Subscription-Key", "")
        if "bad-key" in key:
            return httpx.Response(401, json={"error": "unauthorized"})
        if "timeout" in key:
            raise httpx.ReadTimeout("read timed out")
        return httpx.Response(200, content=b"azure-raw-pcm-bytes")

    good_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    good = AzureTTSProvider(speech_key=SecretStr("good-key"), client=good_client)
    chunks = [c async for c in good.synthesize("hello azure")]
    assert len(chunks) == 1
    assert chunks[0] == b"azure-raw-pcm-bytes"

    bad_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bad = AzureTTSProvider(speech_key=SecretStr("bad-key"), client=bad_client)
    with pytest.raises(TTSPermanentError):
        _ = [c async for c in bad.synthesize("hello")]

    to_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    to = AzureTTSProvider(speech_key=SecretStr("timeout"), client=to_client)
    with pytest.raises(TTSTimeoutError):
        _ = [c async for c in to.synthesize("hello")]


@pytest.mark.asyncio
async def test_fallback_tts_provider() -> None:
    class FailingTTS:
        name = "failing_tts"

        async def synthesize(self, text: str, voice_id: str | None = None) -> Any:
            raise TTSTransientError("TTS service unavailable")
            yield b""  # unreachable

        async def health(self) -> bool:
            return False

    secondary = MockTTSProvider(total_chunks=3)
    metrics = VoiceMetrics()
    fb = FallbackTTSProvider(
        primary=FailingTTS(),  # pyright: ignore[reportArgumentType]
        fallback=secondary,
        circuit_breaker=CircuitBreaker(name="failing_tts", reset_seconds=0.1),
        metrics=metrics,
    )

    chunks = [c async for c in fb.synthesize("test prompt")]
    assert len(chunks) == 3
    assert all(len(c) == 320 for c in chunks)


# ---------------------------------------------------------------------------
# 4. Barge-In / Interruption Handler Tests
# ---------------------------------------------------------------------------
def test_interruption_handler_behavior() -> None:
    cancel_mock = MagicMock()

    cfg = InterruptionConfig(
        energy_threshold=300,
        consecutive_frames_required=2,
    )
    handler = InterruptionHandler(
        stream_sid="MZ123",
        config=cfg,
        on_interrupt=cancel_mock,
    )

    # Low energy audio (silence)
    silence = bytes(320)
    assert not handler.process_incoming_audio(silence)
    assert not handler.is_interrupted
    cancel_mock.assert_not_called()

    # High energy audio while NOT synthesizing: no interruption
    loud_pcm = bytes([100, 50] * 160)
    assert not handler.process_incoming_audio(loud_pcm)
    assert not handler.is_interrupted
    cancel_mock.assert_not_called()

    # Now simulate active TTS synthesis
    handler.set_synthesizing(True)

    # 1 loud frame: under consecutive threshold
    assert not handler.process_incoming_audio(loud_pcm)
    assert not handler.is_interrupted
    cancel_mock.assert_not_called()

    # 2nd loud frame: meets consecutive threshold -> barge-in triggered!
    assert handler.process_incoming_audio(loud_pcm)
    assert handler.is_interrupted
    cancel_mock.assert_called_once()
    assert handler.twilio_clear_message() == {"event": "clear", "streamSid": "MZ123"}

    # Subsequent loud frame while already interrupted does not re-trigger
    assert not handler.process_incoming_audio(loud_pcm)
    assert cancel_mock.call_count == 1


# ---------------------------------------------------------------------------
# 5. VoicePipeline End-to-End Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_voice_pipeline_flow_and_barge_in() -> None:
    sent_messages: list[dict[str, Any]] = []

    async def mock_send(msg: dict[str, Any]) -> None:
        sent_messages.append(msg)

    stt = MockSTTProvider()
    tts = MockTTSProvider(total_chunks=3)

    pipeline = VoicePipeline(
        stream_sid="MZ123",
        stt_provider=stt,
        tts_provider=tts,
        send_frame=mock_send,
        interruption_config=InterruptionConfig(energy_threshold=300, consecutive_frames_required=2),
    )
    await pipeline.start()

    # 1. Process inbound audio frames
    # 160 bytes of mu-law audio
    ulaw_chunk = audioop.lin2ulaw(bytes(320), 2)
    await pipeline.process_inbound_audio(ulaw_chunk)
    assert pipeline.received_frames == 1

    # 2. Trigger speaking
    await pipeline.speak("Thank you for calling.")
    # Verify that TTS generated media frames
    media_frames = [m for m in sent_messages if m.get("event") == "media"]
    assert len(media_frames) == 3
    for frame in media_frames:
        assert frame["streamSid"] == "MZ123"
        assert "payload" in frame["media"]

    # 3. Test barge-in clear message sending
    sent_messages.clear()
    loud_pcm = bytes([120, 80] * 160)
    loud_ulaw = audioop.lin2ulaw(loud_pcm, 2)

    # Start speech in background
    speech_task = asyncio.create_task(pipeline.speak("I am speaking something long"))
    await asyncio.sleep(0.005)

    # Caller interrupts with 2 loud frames
    await pipeline.process_inbound_audio(loud_ulaw)
    await pipeline.process_inbound_audio(loud_ulaw)
    await speech_task

    # Check for "clear" event in sent messages
    clear_events = [m for m in sent_messages if m.get("event") == "clear"]
    assert len(clear_events) >= 1
    assert clear_events[0]["streamSid"] == "MZ123"

    await pipeline.close()


# ---------------------------------------------------------------------------
# 6. Factory Builder Tests
# ---------------------------------------------------------------------------
def test_voice_factory(settings: Settings) -> None:
    # Mock settings with fallback="none"
    cfg_mock = settings.model_copy(
        update={
            "primary_stt_provider": "mock",
            "fallback_stt_provider": "none",
            "primary_tts_provider": "mock",
            "fallback_tts_provider": "none",
        }
    )
    stt = build_stt_provider(cfg_mock)
    tts = build_tts_provider(cfg_mock)
    assert stt.name == "mock"
    assert tts.name == "mock"

    # Fallback config with custom models and voices
    cfg_fallback = settings.model_copy(
        update={
            "primary_stt_provider": "deepgram",
            "fallback_stt_provider": "azure",
            "primary_tts_provider": "elevenlabs",
            "fallback_tts_provider": "azure",
            "deepgram_api_key": SecretStr("test-dg"),
            "deepgram_model": "nova-3",
            "deepgram_language": "fr",
            "azure_speech_key": SecretStr("test-az"),
            "azure_speech_region": "westus",
            "azure_speech_language": "fr-FR",
            "azure_speech_voice": "fr-FR-DeniseNeural",
            "elevenlabs_api_key": SecretStr("test-el"),
            "elevenlabs_model_id": "eleven_multilingual_v2",
            "elevenlabs_voice_id": "custom-voice-xyz",
        }
    )
    stt_fb = build_stt_provider(cfg_fallback)
    tts_fb = build_tts_provider(cfg_fallback)
    assert isinstance(stt_fb, FallbackSTTProvider)
    assert isinstance(tts_fb, FallbackTTSProvider)
    assert stt_fb.name == "deepgram+azure"
    assert tts_fb.name == "elevenlabs+azure"
    assert isinstance(stt_fb.primary, DeepgramSTTProvider)
    assert stt_fb.primary.model == "nova-3"
    assert stt_fb.primary.language == "fr"
    assert isinstance(stt_fb.fallback, AzureSTTProvider)
    assert stt_fb.fallback.language == "fr-FR"
    assert stt_fb.fallback.region == "westus"
    assert isinstance(tts_fb.primary, ElevenLabsTTSProvider)
    assert tts_fb.primary.model_id == "eleven_multilingual_v2"
    assert tts_fb.primary.default_voice_id == "custom-voice-xyz"
    assert isinstance(tts_fb.fallback, AzureTTSProvider)
    assert tts_fb.fallback.default_voice == "fr-FR-DeniseNeural"


# ---------------------------------------------------------------------------
# 7. Telephony run_media WebSocket Pipeline Mode Test
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("agent_enabled", [False, True])
async def test_run_media_pipeline_mode(settings: Settings, agent_enabled: bool) -> None:
    config = voice_settings(settings).model_copy(
        update={
            "telephony_mode": "pipeline",
            "primary_stt_provider": "mock",
            "fallback_stt_provider": "none",
            "primary_tts_provider": "mock",
            "fallback_tts_provider": "none",
        }
    )
    tenant_id = uuid4()
    call_id = uuid4()
    provider_call_id = "CA" + uuid4().hex
    stream_sid = "MZ" + uuid4().hex
    context = CallContext(tenant_id, call_id, provider_call_id, uuid4())

    service = MagicMock()
    service.start = AsyncMock()
    service.finish = AsyncMock()
    service.transfer_number = AsyncMock(return_value=None)
    service.operational_configuration = AsyncMock(return_value=None)

    inbound: asyncio.Queue[Message] = asyncio.Queue()
    outbound: asyncio.Queue[Message] = asyncio.Queue()

    app_mock = MagicMock()
    app_mock.state.resources = None
    app_mock.state.telemetry = None
    app_mock.state.stt_provider = None
    app_mock.state.tts_provider = None
    config = config.model_copy(update={"agent_enabled": agent_enabled})
    from app.agents.providers.mock import MockLLMProvider

    app_mock.state.llm_provider = MockLLMProvider()
    from app.conversation.store import MemoryConversationStateStore

    app_mock.state.conversation_store = MemoryConversationStateStore()
    socket = WebSocket(
        {"type": "websocket", "path": "/", "app": app_mock},
        inbound.get,
        outbound.put,
    )
    await inbound.put({"type": "websocket.connect"})
    await socket.accept()
    await outbound.get()

    async def send(value: dict[str, Any]) -> None:
        await inbound.put({"type": "websocket.receive", "text": json.dumps(value)})

    await send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    await send(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "accountSid": config.twilio_account_sid,
                "callSid": provider_call_id,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "channels": 1,
                },
                "customParameters": {
                    "token": stream_token(config, context),
                },
            },
        }
    )

    task = asyncio.create_task(run_media(socket, service, config, tenant_id, call_id))

    # Send 1 audio frame
    audio = base64.b64encode(b"\xff" * 160).decode()
    await send(
        {
            "event": "media",
            "sequenceNumber": "2",
            "streamSid": stream_sid,
            "media": {"track": "inbound", "payload": audio},
        }
    )

    # Yield to let pipeline process the audio frame
    await asyncio.sleep(0.05)

    # Send stop frame
    await send(
        {
            "event": "stop",
            "sequenceNumber": "3",
            "streamSid": stream_sid,
            "stop": {
                "accountSid": config.twilio_account_sid,
                "callSid": provider_call_id,
            },
        }
    )

    await task

    service.start.assert_awaited_once()
    service.finish.assert_awaited_once()
    finish_args = service.finish.await_args[0]
    assert finish_args[1] == "completed"
    assert finish_args[2] == "stream_stopped"
    assert finish_args[3] == 1
    persisted = await app_mock.state.conversation_store.get(tenant_id, call_id)
    assert persisted is not None and persisted.call_state.status.value == "completed"
    assert persisted.call_state.ended_at is not None
    assert persisted.utterance_count == 1
    if agent_enabled:
        assert persisted.active_intent == "book_appointment"
        assert persisted.last_agent_response != "Test appointment booking"
