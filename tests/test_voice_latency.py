"""No pauses between sentences, quick starts, and the speech services that make that
possible. Everything runs against stand-ins; no speech service is called."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.ai.engine import Done, Event, Say, Working
from app.conversation.manager import ConversationStateManager
from app.conversation.store import MemoryConversationStateStore
from app.voice.phrases import MemoryPhraseCache
from app.voice.pipeline import LEAD_SECONDS, VoicePipeline
from app.voice.receptionist import CallReceptionist
from app.voice.stt.base import STTPermanentError, TranscriptionResult
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.stt.flux import FluxLiveStream
from app.voice.tts.base import TTSRateLimitError
from app.voice.tts.elevenlabs_stream import ElevenLabsSocket
from tests.voice_sim import Line, SilentSTT, TimedTTS

SENTENCES = (
    "Sure, I can help you with that.",
    "Dr. Shah has time on Monday morning.",
    "Would ten thirty work for you?",
)


async def started_pipeline(line: Line, tts: Any, **kwargs: Any) -> VoicePipeline:
    pipeline = VoicePipeline(
        stream_sid="MZ" + "0" * 32,
        stt_provider=SilentSTT(),
        tts_provider=tts,
        send_frame=line.send,
        **kwargs,
    )
    line.pipeline = pipeline
    await pipeline.start()
    return pipeline


@pytest.mark.asyncio
async def test_next_sentence_is_ready_before_the_current_one_ends() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.25)
    pipeline = await started_pipeline(line, tts)
    try:
        # Sentences are prepared as the model writes them, then played in order.
        prepared = [await pipeline.prepare(text) for text in SENTENCES]
        for speech in prepared:
            assert await pipeline.play(speech, follow_on=True)
    finally:
        await pipeline.close()
    # Each sentence waited a quarter of a second for its first audio; the caller hears
    # none of it, because the next sentence was being made while one played.
    assert line.played_gaps() == [] or max(line.played_gaps()) < 0.03
    assert tts.requests == list(SENTENCES)


@pytest.mark.asyncio
async def test_audio_is_sent_a_little_ahead_and_never_far() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.0, seconds_per_char=0.04)
    pipeline = await started_pipeline(line, tts)
    try:
        assert await pipeline.speak(SENTENCES[1], record=False)
    finally:
        await pipeline.close()
    times = line.media_times()
    playing_until, furthest_ahead = 0.0, 0.0
    for at in times:
        furthest_ahead = max(furthest_ahead, playing_until - at)
        playing_until = max(playing_until, at) + 0.02
    # About 1.4 s of audio went out at the pace of the call, never more than the lead
    # ahead of it, which is what a barge-in has to clear.
    assert times[-1] - times[0] > 0.8
    assert furthest_ahead <= LEAD_SECONDS + 0.06


@pytest.mark.asyncio
async def test_a_barge_in_clears_audio_already_sent() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.0)
    pipeline = await started_pipeline(line, tts)
    try:
        assert await pipeline.speak(SENTENCES[0], record=False)
        assert pipeline.audio_pending
        pipeline.interruption.trigger_interruption()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not pipeline.audio_pending
    finally:
        await pipeline.close()
    assert any(message.get("event") == "clear" for _, message in line.sent)


@pytest.mark.asyncio
async def test_the_greeting_is_made_once_then_replayed() -> None:
    cache = MemoryPhraseCache()
    first, second = TimedTTS(first_byte=0.0), TimedTTS(first_byte=0.0)
    for tts in (first, second):
        line = Line()
        pipeline = await started_pipeline(line, tts, phrases=cache)
        try:
            assert await pipeline.speak("Thanks for calling Lotus Clinic.", cache=True)
            await asyncio.sleep(0.01)
        finally:
            await pipeline.close()
        assert line.media_times()
    assert first.requests == ["Thanks for calling Lotus Clinic."]
    assert second.requests == []


class ScriptedEngine:
    """Yields events at the times a real turn would."""

    def __init__(self, script: list[tuple[float, Event]]) -> None:
        self.script = script

    async def run(self, session: Any, state: Any, utterance: str) -> AsyncIterator[Event]:
        for delay, event in self.script:
            await asyncio.sleep(delay)
            yield event


def receptionist_for(engine: ScriptedEngine) -> CallReceptionist:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA" + "0" * 32)
    return CallReceptionist(
        engine=engine,  # type: ignore[arg-type]
        session=MagicMock(),
        state=state,
        store=MemoryConversationStateStore(),
        store_ttl_seconds=60,
        service=MagicMock(),
        sessions=MagicMock(),
        settings=MagicMock(),
        account=MagicMock(),
        metrics=MagicMock(),
    )


@pytest.mark.asyncio
async def test_reply_plays_without_gaps_and_remembers_what_was_said() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.25)
    receptionist = receptionist_for(
        ScriptedEngine(
            [(0.05, Say(SENTENCES[0])), (0.1, Say(SENTENCES[1])), (0.1, Say(SENTENCES[2]))]
        )
    )
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("Can I see Dr. Shah?", True))
    finally:
        await pipeline.close()
    assert line.played_gaps() == [] or max(line.played_gaps()) < 0.03
    assert receptionist.state.transcript_turns[-1].text == " ".join(SENTENCES)


@pytest.mark.asyncio
async def test_a_slow_lookup_gets_one_short_filler_that_is_not_remembered(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(
        ScriptedEngine(
            [
                (0.05, Working("check_availability")),
                (1.2, Say("Monday at ten thirty is free.")),
                (0.0, Done("answered", "Monday at ten thirty is free.")),
            ]
        )
    )
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("Is Monday free?", True))
    finally:
        await pipeline.close()
    assert len(tts.requests) == 2 and tts.requests[1] == "Monday at ten thirty is free."
    assert tts.requests[0] != tts.requests[1]
    assert receptionist.state.transcript_turns[-1].text == "Monday at ten thirty is free."


@pytest.mark.asyncio
async def test_a_quick_lookup_gets_no_filler() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(
        ScriptedEngine(
            [(0.05, Working("list_services")), (0.3, Say("We do cleanings and fillings."))]
        )
    )
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("What do you offer?", True))
    finally:
        await pipeline.close()
    assert tts.requests == ["We do cleanings and fillings."]


# --- Deepgram Flux ---------------------------------------------------------------


class FakeSocket:
    def __init__(self, incoming: list[dict[str, Any]] | None = None) -> None:
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        for message in incoming or []:
            self.incoming.put_nowait(json.dumps(message))
        self.sent: list[str | bytes] = []
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True


def turn(event: str, transcript: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "TurnInfo",
        "event": event,
        "turn_index": 0,
        "audio_window_start": 0,
        "audio_window_end": 1,
        "transcript": transcript,
        "words": [{"word": w, "confidence": 0.9} for w in transcript.split()],
        "end_of_turn_confidence": 0.8,
        **extra,
    }


@pytest.mark.asyncio
async def test_flux_turns_become_interim_and_final_results() -> None:
    socket = FakeSocket(
        [
            {"type": "Connected", "request_id": "r", "sequence_id": 0},
            turn("StartOfTurn", ""),
            turn("Update", "I'd like to"),
            turn("EndOfTurn", "I'd like to book", languages=["hi", "en"]),
        ]
    )
    stream = FluxLiveStream(socket, 5)
    results: list[TranscriptionResult] = []
    async for result in stream.results():
        results.append(result)
        if result.is_final:
            break
    assert results[0].speech_started and not results[0].is_final
    assert results[1].text == "I'd like to" and not results[1].is_final
    final = results[2]
    assert final.is_final and final.text == "I'd like to book"
    assert final.language == "hi" and final.languages == ("hi", "en")
    assert final.confidence == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_flux_receives_80ms_of_audio_per_message_and_closes_cleanly() -> None:
    socket = FakeSocket()
    stream = FluxLiveStream(socket, 5)
    for _ in range(8):
        await stream.send_audio(bytes(320))
    assert [len(item) for item in socket.sent] == [1280, 1280]
    await stream.close()
    assert socket.sent[-1] == '{"type":"CloseStream"}' and socket.closed


def query(provider: DeepgramSTTProvider) -> tuple[str, dict[str, list[str]]]:
    path, params = provider._live_query(8000, "linear16")  # pyright: ignore[reportPrivateUsage]
    return path, parse_qs(params)


def test_flux_query_follows_the_clinic_languages_and_names() -> None:
    key = SecretStr("dg")
    path, english = query(
        DeepgramSTTProvider(key, model="flux", language="en", keyterms=("Lotus Clinic", "Dr. Shah"))
    )
    assert path == "/v2/listen"
    assert english["model"] == ["flux-general-en"]
    assert english["keyterm"] == ["Lotus Clinic", "Dr. Shah"]
    assert "language_hint" not in english
    _, multi = query(
        DeepgramSTTProvider(key, model="flux", language="multi", language_hints=("en", "hi", "gu"))
    )
    # Gujarati is not a Flux language, so it is not hinted.
    assert multi["model"] == ["flux-general-multi"] and multi["language_hint"] == ["en", "hi"]
    with pytest.raises(STTPermanentError):
        query(DeepgramSTTProvider(key, model="flux", language="gu"))
    path, nova = query(DeepgramSTTProvider(key, model="nova-3", language="gu", keyterms=("Lotus",)))
    assert path == "/v1/listen" and nova["endpointing"] == ["500"] and nova["keyterm"] == ["Lotus"]


# --- ElevenLabs socket -----------------------------------------------------------


class ElevenLabsServer:
    """Answers each flushed context with two audio chunks and a final message."""

    def __init__(self, *, fail: str | None = None, hold: bool = False) -> None:
        self.fail = fail
        self.hold = hold
        self.url = ""
        self.socket = FakeSocket()
        original = self.socket.send

        async def send(message: str | bytes) -> None:
            await original(message)
            data = json.loads(message)
            if not data.get("flush") or self.hold:
                return
            context = data["context_id"]
            if self.fail:
                self.socket.incoming.put_nowait(json.dumps({"error": self.fail}))
                return
            for chunk in (b"\x01" * 160, b"\x02" * 80):
                self.socket.incoming.put_nowait(
                    json.dumps({"audio": base64.b64encode(chunk).decode(), "contextId": context})
                )
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
        timeout_seconds=1,
        connector=server.connect,
    )


@pytest.mark.asyncio
async def test_elevenlabs_socket_streams_each_sentence_as_ulaw() -> None:
    server = ElevenLabsServer()
    socket = eleven(server)
    first = b"".join([chunk async for chunk in socket.speak("Hello there.")])
    second = b"".join([chunk async for chunk in socket.speak("How can I help?")])
    await socket.close()
    assert first == second == b"\x01" * 160 + b"\x02" * 80
    params = parse_qs(urlsplit(server.url).query)
    assert params["output_format"] == ["ulaw_8000"] and params["model_id"] == ["eleven_flash_v2_5"]
    assert params["language_code"] == ["hi"] and params["auto_mode"] == ["true"]
    texts = [json.loads(item) for item in server.socket.sent]
    assert {"text": "Hello there. ", "context_id": texts[1]["context_id"], "flush": True} in texts
    assert texts[-1] == {"close_socket": True}


@pytest.mark.asyncio
async def test_elevenlabs_context_is_closed_when_the_caller_talks_over_it() -> None:
    server = ElevenLabsServer()
    socket = eleven(server)
    speech = socket.speak("This sentence is cut short.")
    assert await anext(speech)
    await speech.aclose()
    sent = [json.loads(item) for item in server.socket.sent]
    assert sent[-1].get("close_context") is True
    await socket.close()


@pytest.mark.asyncio
async def test_elevenlabs_errors_reach_the_sentence() -> None:
    socket = eleven(ElevenLabsServer(fail="Too many concurrent requests (rate limit)"))
    with pytest.raises(TTSRateLimitError):
        _ = [chunk async for chunk in socket.speak("Hello.")]
    await socket.close()
