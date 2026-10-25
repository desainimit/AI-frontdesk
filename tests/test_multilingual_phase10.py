import json
from collections.abc import AsyncGenerator
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.ai.wording import spoken
from app.conversation.confirmation import confirmation_answer
from app.conversation.language import observe_language
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConfirmationStatus
from app.voice.pipeline import VoicePipeline
from app.voice.stt.base import TranscriptionResult
from app.voice.stt.live import DeepgramLiveStream
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider
from tests.confirmations import ask_to_confirm
from tests.fake_stt import MockSTTProvider
from tests.fake_tts import MockTTSProvider


def test_hindi_hinglish_and_sustained_english_switch() -> None:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CAphase10")
    hindi = (
        "\u092e\u0941\u091d\u0947 \u0915\u0932 \u0921\u0949\u0915\u094d\u091f\u0930 "
        "\u0938\u0947 \u092e\u093f\u0932\u0928\u093e \u0939\u0948"
    )
    ConversationStateManager.process_caller_utterance(state, hindi, confidence=0.99)
    assert state.language == "hi"
    ConversationStateManager.process_caller_utterance(
        state,
        "Mujhe kal doctor appointment chahiye",
        detected_languages=("hi", "en"),
        confidence=0.99,
    )
    assert state.language == "hi"
    assert state.language_state.code_switching
    ConversationStateManager.process_caller_utterance(state, "yes", detected_language="en")
    assert state.language == "hi"
    ConversationStateManager.process_caller_utterance(state, "Actually Monday is better")
    assert state.language == "hi"
    ConversationStateManager.process_caller_utterance(state, "Please book it for Monday")
    assert state.language == "en"


def test_transcript_confidence_is_not_language_confidence() -> None:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CAphase10")
    ConversationStateManager.process_caller_utterance(
        state, "Mujhe doctor appointment chahiye", detected_language="hi", confidence=0.01
    )
    assert state.language == "hinglish"
    assert observe_language("haan", "hi") is None
    assert observe_language("Doctor se milna hai", "hi").language == "hinglish"  # type: ignore[union-attr]


class SpeechSocketFake:
    def __init__(self, messages: list[str]) -> None:
        self.messages = iter(messages)

    async def send(self, message: str | bytes) -> None:
        pass

    async def recv(self) -> str:
        return next(self.messages)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_multilingual_stream_does_not_emit_configured_multi_as_observed_language() -> None:
    messages = [
        json.dumps(
            {
                "type": "Results",
                "channel": {
                    "alternatives": [
                        {"transcript": "Mujhe appointment chahiye", "confidence": 0.98}
                    ]
                },
                "is_final": True,
                "speech_final": True,
            }
        ),
        json.dumps(
            {
                "type": "Results",
                "channel": {
                    "detected_language": "hi",
                    "alternatives": [
                        {"transcript": "\u0928\u092e\u0938\u094d\u0924\u0947", "confidence": 0.9}
                    ],
                },
                "is_final": True,
                "speech_final": True,
            }
        ),
    ]
    stream = DeepgramLiveStream(SpeechSocketFake(messages), "multi", 1)
    results = cast(AsyncGenerator[TranscriptionResult, None], stream.results())
    first = await anext(results)
    second = await anext(results)
    await results.aclose()
    await stream.close()
    assert first.language is None
    assert second.language == "hi"


@pytest.mark.asyncio
async def test_tts_language_and_fallback_routing() -> None:
    payloads: list[dict[str, object]] = []
    ssml: list[str] = []

    def eleven(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, content=b"\x00\x00")

    def azure(request: httpx.Request) -> httpx.Response:
        ssml.append(request.content.decode())
        return httpx.Response(200, content=b"\x00\x00")

    async with httpx.AsyncClient(transport=httpx.MockTransport(eleven)) as client:
        provider = ElevenLabsTTSProvider(SecretStr("key"), client=client)
        _ = [chunk async for chunk in provider.synthesize("Namaste", language="hi")]
        _ = [chunk async for chunk in provider.synthesize("Mujhe appointment", language="hinglish")]
    assert payloads[0]["language_code"] == "hi"
    assert "language_code" not in payloads[1]

    async with httpx.AsyncClient(transport=httpx.MockTransport(azure)) as client:
        provider = AzureTTSProvider(SecretStr("key"), client=client)
        _ = [chunk async for chunk in provider.synthesize("Namaste", language="hi")]
    assert "hi-IN-SwaraNeural" in ssml[0]
    assert "hi-IN" in ssml[0]


def test_hindi_confirmation_and_server_owned_speech() -> None:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CAphase10")
    state.language_state.primary_language = "hi"
    ask_to_confirm(state, {"slot_time": "10:00"}, "Confirm 10:00?")
    assert confirmation_answer("\u0939\u093e\u0901", "hi") is True
    assert confirmation_answer("\u0928\u0939\u0940\u0902", "en") is not True
    ConversationStateManager.process_caller_utterance(state, "\u0939\u093e\u0901")
    assert state.confirmation_state.status == ConfirmationStatus.CONFIRMED
    assert "\u0905\u092a\u0949\u0907\u0902\u091f\u092e\u0947\u0902\u091f" in spoken(
        "hi", "booked", time="10:00", reference="A-7"
    )
    assert "appointment" in spoken("hinglish", "booked", time="10:00", reference="A-7")


@pytest.mark.asyncio
async def test_pipeline_speaks_using_stable_language() -> None:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CAphase10")
    state.language_state.primary_language = "hi"
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    tts = MockTTSProvider(total_chunks=1)
    pipeline = VoicePipeline(
        "MZphase10",
        MockSTTProvider(),
        tts,
        send,
        conversation_state=state,
        multilingual_enabled=True,
    )
    await pipeline.start()
    try:
        await pipeline.speak("Namaste")
        assert tts.last_language == "hi"
        assert any(frame["event"] == "media" for frame in sent)
    finally:
        await pipeline.close()
