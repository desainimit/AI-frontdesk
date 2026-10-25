"""The rest of a call: silence, goodbyes, after-hours call-backs, timing, stopping."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Response

from app.ai.engine import Done, Event, Say
from app.ai.llm.base import ToolCall
from app.ai.wording import spoken
from app.api.health import ready
from app.api.telephony import needs_call_back
from app.calls.service import CallContext
from app.core.drain import DRAIN
from app.safety import handover
from app.safety.transfer import hang_up
from app.settings.defaults import default_settings
from app.voice.stt.base import TranscriptionResult
from tests.test_telephony import voice_settings
from tests.test_voice_interruptions import Engine, call, until
from tests.test_voice_latency import ScriptedEngine, receptionist_for, started_pipeline
from tests.test_voice_transfer import call as voice_call
from tests.test_voice_transfer import clinic, voice_engine
from tests.test_voice_transfer import turn as voice_turn
from tests.voice_sim import Line, TimedTTS


async def test_silence_is_checked_then_the_call_is_let_go(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.pipeline.SILENCE_SECONDS", 0.3)
    hung_up = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.hang_up", hung_up)
    pipeline, receptionist, _, line = await call(Engine())
    pipeline.on_silence = receptionist.silence
    pipeline._silence_task = asyncio.create_task(pipeline._watch_silence())  # pyright: ignore[reportPrivateUsage]
    try:
        await until(lambda: hung_up.await_count == 1, limit=8)
    finally:
        await pipeline.close()
    said = [turn.text for turn in receptionist.state.transcript_turns if turn.speaker == "agent"]
    assert said == [spoken("en", "still_there"), spoken("en", "goodbye_silent")]
    assert receptionist.ended and line.media_times()


async def test_a_caller_who_speaks_is_not_asked_whether_they_are_there(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.pipeline.SILENCE_SECONDS", 0.4)
    asked = AsyncMock()
    pipeline, _, stt, _ = await call(Engine())
    pipeline.on_silence = asked
    pipeline._silence_task = asyncio.create_task(pipeline._watch_silence())  # pyright: ignore[reportPrivateUsage]
    try:
        for _ in range(5):
            await asyncio.sleep(0.15)
            stt.stream.say("so I was thinking", final=False)
    finally:
        await pipeline.close()
    asked.assert_not_awaited()


async def test_the_model_ends_the_call_after_its_goodbye() -> None:
    built = voice_engine([ToolCall("c1", "end_call", {})], "Thanks for calling, take care!")
    session, state = voice_call(clinic())
    said, _, done = await voice_turn(built, session, state, "That's all, bye!")
    assert said == ["Thanks for calling, take care!"] and state.end_requested
    assert done.tools == ("end_call",)


async def test_the_call_is_not_ended_while_a_question_is_open() -> None:
    built = voice_engine(
        [ToolCall("c1", "end_call", {})], "Before you go: shall I put you through?"
    )
    session, state = voice_call(clinic())
    # The receptionist has just offered to put the caller through.
    handover.offer(state, None, "My bill is wrong")
    _, _, done = await voice_turn(built, session, state, "Bye")
    assert done.tools == ("end_call",) and not state.end_requested


async def test_the_receptionist_hangs_up_once_the_goodbye_was_heard(monkeypatch: Any) -> None:
    hung_up = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.hang_up", hung_up)
    record = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.record_turn", record)

    class Goodbye(ScriptedEngine):
        async def run(self, session: Any, state: Any, utterance: str) -> AsyncIterator[Event]:
            await asyncio.sleep(0.05)
            state.end_requested = True
            yield Say("Thanks for calling, take care!")
            yield Done("answered", "Thanks for calling, take care!")

    receptionist = receptionist_for(Goodbye([]))
    line = Line()
    pipeline = await started_pipeline(line, TimedTTS(first_byte=0.05))
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("Bye!", True))
    finally:
        await pipeline.close()
    hung_up.assert_awaited_once()
    # How long the caller waited for the first sound of the reply is kept with the turn.
    first_audio = record.await_args.kwargs["first_audio_ms"]  # type: ignore[union-attr]
    assert 50 <= first_audio < 1000 and record.await_args.kwargs["interrupted"] is False  # type: ignore[union-attr]


async def test_hanging_up_asks_twilio_to_end_the_call(settings: Any) -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    config = voice_settings(settings)
    context = CallContext(uuid4(), uuid4(), "CA" + "3" * 32, uuid4())
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        await hang_up(config, context, client=client)
    assert sent[0].url.path.endswith(f"/Calls/{context.provider_call_id}.json")
    assert b"Hangup" in sent[0].content


def test_after_hours_calls_are_called_back_only_when_needed() -> None:
    receptionist = receptionist_for(ScriptedEngine([]))
    state = receptionist.state
    state.utterance_count = 2
    # Sunday 23:00 in Kolkata: closed.
    state.created_at = datetime(2026, 10, 25, 17, 30, tzinfo=UTC)
    base = default_settings("Lotus Clinic")
    callback = base.model_copy(
        update={"hours": base.hours.model_copy(update={"after_hours": "callback"})}
    )
    assert needs_call_back(callback, state, receptionist)
    assert not needs_call_back(base, state, receptionist)  # the clinic did not ask for it
    receptionist.tickets = 1
    assert not needs_call_back(callback, state, receptionist)  # the team has it already


async def test_a_stopping_server_takes_no_new_calls() -> None:
    resources = MagicMock()
    resources.database.ping = AsyncMock()
    resources.redis.ping = AsyncMock()
    request = MagicMock()
    request.app.state.settings.dependency_timeout_seconds = 1
    response = Response()
    assert (await ready(request, response, resources)).status == "ready"
    DRAIN.draining = True
    try:
        with DRAIN.call():
            assert DRAIN.calls == 1
            stopping = await ready(request, response, resources)
        assert stopping.status == "not_ready" and response.status_code == 503
        assert DRAIN.calls == 0
    finally:
        DRAIN.draining = False


@pytest.mark.parametrize("times", [1])
async def test_still_there_is_asked_in_the_callers_language(times: int) -> None:
    pipeline, receptionist, _, line = await call(Engine())
    receptionist.state.language_state.primary_language = "hi"
    try:
        await receptionist.silence(times)
    finally:
        await pipeline.close()
    assert receptionist.state.transcript_turns[-1].text == spoken("hi", "still_there")
    assert line.media_times()
