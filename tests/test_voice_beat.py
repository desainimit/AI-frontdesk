"""Answering within a beat, the ElevenLabs socket that keeps speech flowing, and the
numbers given in an emergency. Everything runs against stand-ins."""

import base64
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr

from app.ai.engine import Done, Say
from app.safety.messages import emergency_message
from app.safety.numbers import emergency_lines
from app.voice.acknowledge import acknowledgement, after_acknowledgement
from app.voice.opening import opening
from app.voice.stt.base import TranscriptionResult
from app.voice.tts.base import TTSRateLimitError
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider
from app.voice.tts.elevenlabs_stream import ElevenLabsSocket
from tests.test_voice_latency import (
    FakeSocket,
    ScriptedEngine,
    receptionist_for,
    started_pipeline,
)
from tests.voice_sim import Line, TimedTTS

# --- Answering within a beat -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reply_after_an_acknowledgement_does_not_open_with_another(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(
        ScriptedEngine(
            [
                (0.5, Say("Sure, can I get your name?")),
                (0.0, Done("answered", "Sure, can I get your name?")),
            ]
        )
    )
    metrics = receptionist.metrics = MagicMock()
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("I need to move my appointment.", True))
    finally:
        await pipeline.close()
    assert tts.requests == ["Okay,", "Can I get your name?"]
    # The caller heard a sound long before the answer was ready.
    assert metrics.record_first_audio.call_args.args[0] < 400


@pytest.mark.asyncio
async def test_a_quick_reply_needs_no_acknowledgement() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(ScriptedEngine([(0.05, Say("Sure, we're open till five."))]))
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("Are you open today?", True))
    finally:
        await pipeline.close()
    assert tts.requests == ["Sure, we're open till five."]


def test_acknowledgements_vary_and_stay_quiet_for_someone_in_pain() -> None:
    first = acknowledgement("en", "I'd like to book a cleaning.", 0, None)
    assert first and acknowledgement("en", "Tomorrow morning.", 0, first) != first
    assert acknowledgement("en", "Are you open Saturday?", 0, None) == "Mm,"
    assert acknowledgement("en", "My tooth hurts so much", 0, None) is None
    assert acknowledgement("hi", "मुझे अपॉइंटमेंट चाहिए", 0, None) == "जी,"


def test_only_a_repeated_acknowledgement_is_taken_off() -> None:
    assert after_acknowledgement("Got it! Thanks, Maria.", "en") == "Thanks, Maria."
    assert after_acknowledgement("Okay.", "en") == ""
    # "Yes" can be the answer itself; "Sorry" is not an acknowledgement.
    assert after_acknowledgement("Yes, we're open.", "en") == "Yes, we're open."
    assert after_acknowledgement("Sorry, Friday is full.", "en") == "Sorry, Friday is full."
    assert after_acknowledgement("जी, आपका नाम बताइए?", "hi") == "आपका नाम बताइए?"


# --- ElevenLabs socket -------------------------------------------------------------


class ElevenLabsServer:
    """Behaves like ElevenLabs: audio after a flush, the final message only once the
    context is closed (waiting for it without closing is what timed calls out)."""

    def __init__(self, *, fail: str | None = None, silent: bool = False) -> None:
        self.fail = fail
        self.silent = silent
        self.url = ""
        self.socket = FakeSocket()
        original = self.socket.send

        async def send(message: str | bytes) -> None:
            await original(message)
            data = json.loads(message)
            context = data.get("context_id")
            if self.silent:
                return
            if data.get("flush"):
                if self.fail:
                    self.socket.incoming.put_nowait(json.dumps({"error": self.fail}))
                    return
                for chunk in (b"\x01" * 160, b"\x02" * 80):
                    self.socket.incoming.put_nowait(
                        json.dumps(
                            {"audio": base64.b64encode(chunk).decode(), "contextId": context}
                        )
                    )
            if data.get("close_context") and not self.fail:
                self.socket.incoming.put_nowait(json.dumps({"isFinal": True, "contextId": context}))

        self.socket.send = send  # type: ignore[method-assign]

    async def connect(self, url: str, **_: Any) -> FakeSocket:
        self.url = url
        return self.socket


def eleven(server: ElevenLabsServer) -> ElevenLabsSocket:
    return ElevenLabsSocket(
        api_key="el",
        voice_id="voice1234567890abcdef",
        model_id="eleven_flash_v2_5",
        language_code="hi",
        voice_settings={"stability": 0.5},
        timeout_seconds=0.5,
        connector=server.connect,
    )


@pytest.mark.asyncio
async def test_elevenlabs_socket_closes_each_sentence_so_it_finishes() -> None:
    server = ElevenLabsServer()
    socket = eleven(server)
    first = b"".join([chunk async for chunk in socket.speak("Hello there.")])
    second = b"".join([chunk async for chunk in socket.speak("How can I help?")])
    await socket.close()
    assert first == second == b"\x01" * 160 + b"\x02" * 80
    params = parse_qs(urlsplit(server.url).query)
    assert params["output_format"] == ["ulaw_8000"] and params["language_code"] == ["hi"]
    sent = [json.loads(item) for item in server.socket.sent]
    context = sent[1]["context_id"]
    assert sent[1:3] == [
        {"text": "Hello there. ", "context_id": context, "flush": True},
        {"context_id": context, "close_context": True},
    ]
    assert sent[-1] == {"close_socket": True}


@pytest.mark.asyncio
async def test_elevenlabs_errors_reach_the_sentence() -> None:
    socket = eleven(ElevenLabsServer(fail="Too many concurrent requests (rate limit)"))
    with pytest.raises(TTSRateLimitError):
        _ = [chunk async for chunk in socket.speak("Hello.")]
    await socket.close()


@pytest.mark.asyncio
async def test_a_failing_socket_falls_back_to_http_for_the_rest_of_the_call() -> None:
    server = ElevenLabsServer(silent=True)
    provider = ElevenLabsTTSProvider(SecretStr("el"), timeout_seconds=0.3)
    provider._socket = eleven(server)  # pyright: ignore[reportPrivateUsage]
    provider._socket_key = (provider.default_voice_id, None)  # pyright: ignore[reportPrivateUsage]
    pcm: list[str] = []

    async def http(text: str, *_: Any, **__: Any) -> Any:
        pcm.append(text)
        yield b"\x00\x00" * 160

    provider.synthesize = http  # type: ignore[method-assign]
    audio = b"".join([chunk async for chunk in provider.synthesize_ulaw("Hello.")])
    again = b"".join([chunk async for chunk in provider.synthesize_ulaw("Still here.")])
    await provider.aclose()
    assert len(audio) == len(again) == 160 and provider.socket_failed
    assert pcm == ["Hello.", "Still here."]


# --- Emergencies -------------------------------------------------------------------


def test_emergency_numbers_follow_the_clinics_country() -> None:
    india = emergency_lines("+912212345678", "Asia/Kolkata")
    assert "1 1 2" in emergency_message("en", india, "chest_pain")
    crisis = emergency_message("hi", india, "self_harm")
    assert "1 4 4 1 6" in crisis and "1 1 2" in crisis
    assert "9 1 1" in emergency_message("en", emergency_lines("", "America/Chicago"), None)
    unknown = emergency_message("en", emergency_lines("", "Asia/Tokyo"), None)
    assert "local emergency number" in unknown


# --- The opening ---------------------------------------------------------------------


def test_the_opening_is_said_in_a_receptionists_order() -> None:
    greeting = "Thank you for calling Sunrise Clinic. How can I help you today?"
    closed = "Just so you know, we're closed right now."
    lines = opening(greeting, hello="Hi Nimit!", closed=closed)
    assert [line.text for line in lines] == [
        "Hi Nimit!",
        "Thank you for calling Sunrise Clinic.",
        closed,
        "How can I help you today?",
    ]
    assert [line.record for line in lines] == [False, True, True, True]
    # Open, and a new caller: just the greeting, as one line.
    assert [line.text for line in opening(greeting, hello=None, closed=None)] == [greeting]


@pytest.mark.asyncio
async def test_opening_lines_follow_each_other_with_a_beat_and_cut_nothing_off() -> None:
    lines = opening("Thank you for calling.", hello="Hi Nimit!", closed=None)
    frames: list[int] = []
    for beat in (0.0, 0.3):
        line, tts = Line(), TimedTTS(first_byte=0.1)
        pipeline = await started_pipeline(line, tts)
        try:
            assert await pipeline.speak_lines(lines, beat=beat)
        finally:
            await pipeline.close()
        # Before, each line cleared the end of the one before it off the line.
        assert not line.cleared()
        assert tts.requests == ["Hi Nimit!", "Thank you for calling."]
        frames.append(len(line.media_times()))
    # A beat of 0.3 s between the two lines: fifteen 20 ms frames of silence.
    assert frames[1] - frames[0] == 15
