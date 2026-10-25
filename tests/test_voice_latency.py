"""No pauses between sentences, quick starts, and the speech services that make that
possible. Everything runs against stand-ins; no speech service is called."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from app.ai.engine import Done, Event, Say, Working
from app.ai.speech_text import speakable
from app.conversation.manager import ConversationStateManager
from app.conversation.store import MemoryConversationStateStore
from app.voice.phrases import MemoryPhraseCache
from app.voice.pipeline import LEAD_SECONDS, VoicePipeline
from app.voice.receptionist import CallReceptionist
from app.voice.stt.base import STTPermanentError, TranscriptionResult
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.stt.flux import FluxLiveStream
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
    assert tts.requests == [speakable(text) for text in SENTENCES]


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
async def test_a_lookup_is_said_aloud_like_a_person_thinking(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    line, tts = Line(), TimedTTS(first_byte=0.0)
    friday = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=4)).isoformat()
    lookup = {"provider_id": "p1", "starts_at": friday}
    receptionist = receptionist_for(
        ScriptedEngine(
            [
                (0.05, Working("check_availability", lookup)),
                (0.6, Say("Monday at ten thirty is free.")),
                (0.0, Done("answered", "Monday at ten thirty is free.")),
            ]
        )
    )
    receptionist.session = MagicMock(
        directory={"doctors": [{"id": "p1", "name": "Dr. Rao"}]},
        configuration=MagicMock(clinic=MagicMock(timezone="Asia/Kolkata")),
    )
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("Is Friday free?", True))
        # The same lookup again: it is said differently the second time.
        receptionist.engine = ScriptedEngine(  # type: ignore[assignment]
            [(0.05, Working("check_availability", lookup)), (0.3, Say("Ten is free too."))]
        )
        await receptionist.reply(TranscriptionResult("And Friday later?", True))
    finally:
        await pipeline.close()
    first, _, second, _ = tts.requests
    assert first.startswith("Let me see what Doctor Rao has on ")  # said as it starts
    assert second != first and "one sec" not in (first + second).lower()
    # Thinking aloud is not part of the reply that is remembered.
    assert receptionist.state.transcript_turns[-1].text == "Ten is free too."


@pytest.mark.asyncio
async def test_a_speech_failure_does_not_end_the_call(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    line, tts = Line(), TimedTTS(first_byte=0.0, fail_on="broken")
    receptionist = receptionist_for(
        ScriptedEngine([(0.05, Say("This sentence is broken.")), (0.0, Done("answered", ""))])
    )
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        # Before, the error escaped the reply and the call was dropped.
        await receptionist.reply(TranscriptionResult("Hello?", True))
        receptionist.engine = ScriptedEngine([(0.05, Say("Sorry, how can I help?"))])  # type: ignore[assignment]
        await receptionist.reply(TranscriptionResult("Hello?", True))
    finally:
        await pipeline.close()
    assert receptionist.state.transcript_turns[-1].text == "Sorry, how can I help?"


@pytest.mark.asyncio
async def test_a_short_pause_before_answering_is_not_filled() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(ScriptedEngine([(1.8, Say("We do cleanings and fillings."))]))
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("What do you offer?", True))
    finally:
        await pipeline.close()
    # Nearly two seconds of thinking is a normal pause: no "one second" before it.
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
