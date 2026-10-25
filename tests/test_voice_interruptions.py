"""Talking over the receptionist: what stops it, what does not, and what it remembers."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.ai.engine import Event, Say
from app.conversation.backchannel import is_backchannel
from app.conversation.manager import ConversationStateManager
from app.conversation.store import MemoryConversationStateStore
from app.voice.pipeline import VoicePipeline
from app.voice.processors.interruption import ENERGY_REASON
from app.voice.receptionist import CallReceptionist
from tests.voice_sim import Line, ScriptedSTT, TimedTTS

FIRST = "Sure, I can help you with that today."
SECOND = "Dr. Shah has time on Monday morning at ten."
THIRD = "She also has Wednesday afternoon at four."


class Engine:
    """Answers every turn from a script and notes what it was asked and knew."""

    def __init__(self, *replies: tuple[str, ...]) -> None:
        self.replies = list(replies)
        self.asked: list[str] = []
        self.knew_cut_off: list[bool] = []

    async def run(self, session: Any, state: Any, utterance: str) -> AsyncIterator[Event]:
        self.asked.append(utterance)
        self.knew_cut_off.append(state.reply_cut_off)
        for sentence in self.replies.pop(0) if self.replies else ("Okay.",):
            await asyncio.sleep(0.02)
            yield Say(sentence)


async def call(engine: Engine) -> tuple[VoicePipeline, CallReceptionist, ScriptedSTT, Line]:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA" + "0" * 32)
    receptionist = CallReceptionist(
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
    line, stt = Line(), ScriptedSTT()
    pipeline = VoicePipeline(
        "MZ" + "0" * 32,
        stt,
        TimedTTS(first_byte=0.0),
        line.send,
        on_transcript=receptionist.reply,
        on_resume=receptionist.resume,
        conversation_state=state,
    )
    line.pipeline = pipeline
    receptionist.pipeline = pipeline
    await pipeline.start()
    return pipeline, receptionist, stt, line


async def until(condition: Any, limit: float = 6.0) -> None:
    async with asyncio.timeout(limit):
        # The state belongs to the call's own tasks; a test can only look at it.
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.02)


def agent_turns(receptionist: CallReceptionist) -> list[str]:
    return [turn.text for turn in receptionist.state.transcript_turns if turn.speaker == "agent"]


@pytest.mark.asyncio
async def test_mm_hmm_does_not_stop_the_reply() -> None:
    engine = Engine((FIRST, SECOND))
    pipeline, receptionist, stt, line = await call(engine)
    try:
        stt.stream.say("Can I see Dr. Shah?")
        await asyncio.sleep(0.5)
        stt.stream.say("mm-hmm", final=False)
        stt.stream.say("Mm-hmm.")
        await until(lambda: agent_turns(receptionist))
        await asyncio.sleep(0.2)
    finally:
        await pipeline.close()
    assert not line.cleared()
    assert agent_turns(receptionist) == [f"{FIRST} {SECOND}"]
    # "Mm-hmm" after a statement needed no answer.
    assert engine.asked == ["Can I see Dr. Shah?"]


@pytest.mark.asyncio
async def test_yes_said_over_a_question_is_answered_after_it() -> None:
    engine = Engine(("Monday at ten is free. Shall I book it?",), ("Booked.",))
    pipeline, _, stt, _ = await call(engine)
    try:
        stt.stream.say("Is Monday free?")
        await asyncio.sleep(0.4)
        stt.stream.say("Yes.")
        await until(lambda: len(engine.asked) == 2)
    finally:
        await pipeline.close()
    assert engine.asked == ["Is Monday free?", "Yes."]


@pytest.mark.asyncio
async def test_words_stop_the_reply_and_only_what_was_heard_is_remembered() -> None:
    engine = Engine((FIRST, SECOND, THIRD), ("Tuesday works too.",))
    pipeline, receptionist, stt, line = await call(engine)
    try:
        stt.stream.say("When can I see Dr. Shah?")
        # The first sentence takes about 0.7 s to play; stop the second one midway.
        await asyncio.sleep(1.2)
        stt.stream.say("wait, actually", final=False)
        await until(line.cleared)
        await until(lambda: receptionist.state.reply_cut_off)
        heard = agent_turns(receptionist)[-1]
        assert heard.startswith(FIRST + " ") and heard.endswith(" —")
        assert SECOND not in heard and THIRD not in heard
        assert receptionist.state.unsaid == f"{SECOND} {THIRD}"
        stt.stream.say("Wait, actually I need Tuesday.")
        await until(lambda: len(engine.asked) == 2)
        await until(lambda: not receptionist.state.reply_cut_off)
    finally:
        await pipeline.close()
    # The model was told it had been cut off when answering the interruption.
    assert engine.knew_cut_off == [False, True]


@pytest.mark.asyncio
async def test_a_noise_without_words_lets_the_reply_carry_on(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.pipeline.RESUME_AFTER_SECONDS", 0.3)
    engine = Engine((FIRST, SECOND, THIRD))
    pipeline, receptionist, stt, line = await call(engine)
    try:
        stt.stream.say("When can I see Dr. Shah?")
        await asyncio.sleep(1.2)
        # A door slams: loud enough and long enough to stop the receptionist.
        pipeline.interruption.trigger_interruption(reason=ENERGY_REASON)
        await until(lambda: receptionist.state.reply_cut_off)
        await until(lambda: not receptionist.state.reply_cut_off)
    finally:
        await pipeline.close()
    assert line.cleared()
    turns = agent_turns(receptionist)
    # It picks up from the sentence that was cut, without asking the model again.
    assert turns[-1] == f"{SECOND} {THIRD}"
    assert engine.asked == ["When can I see Dr. Shah?"]


@pytest.mark.asyncio
async def test_words_after_a_noise_mean_no_resume(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.pipeline.RESUME_AFTER_SECONDS", 0.3)
    engine = Engine((FIRST, SECOND, THIRD), ("Of course.",))
    pipeline, receptionist, stt, _ = await call(engine)
    try:
        stt.stream.say("When can I see Dr. Shah?")
        await asyncio.sleep(1.2)
        pipeline.interruption.trigger_interruption(reason=ENERGY_REASON)
        stt.stream.say("sorry, can you", final=False)
        await asyncio.sleep(0.5)
        stt.stream.say("Sorry, can you repeat that?")
        await until(lambda: len(engine.asked) == 2)
        await asyncio.sleep(0.2)
    finally:
        await pipeline.close()
    assert engine.asked[1] == "Sorry, can you repeat that?"
    assert all(turn != f"{SECOND} {THIRD}" for turn in agent_turns(receptionist))


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("Mm-hmm.", "en", True),
        ("okay", "en", True),
        ("Yeah, right", "en", True),
        ("haan ji", "hinglish", True),
        ("जी", "hi", True),
        ("હા", "gu", True),
        ("d'accord", "fr", True),
        ("no", "en", False),
        ("wait", "en", False),
        ("okay so what about Tuesday", "en", False),
        ("", "en", False),
    ],
)
def test_backchannels(text: str, language: str, expected: bool) -> None:
    assert is_backchannel(text, language) is expected
