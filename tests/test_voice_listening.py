"""Listening for the end of a sentence, a slow model start, and the hand-over loop seen
on a real call. Everything runs against stand-ins; no provider is called."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.ai.engine import Handoff
from app.ai.llm.base import ChatRequest, Completed, ModelEvent, TextDelta
from app.ai.llm.gateway import ModelGateway
from app.conversation.state import SafetyDecision
from app.conversation.turn_end import sounds_unfinished
from app.safety.handover import consent
from app.voice.pipeline import VoicePipeline
from app.voice.stt.base import TranscriptionResult
from tests.test_voice_latency import ScriptedEngine, receptionist_for
from tests.voice_sim import Line, ScriptedSTT, TimedTTS

# --- The end of a sentence -----------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "unfinished"),
    [
        ("To check my", True),
        ("I just got the message about the", True),
        ("I was thinking...", True),
        ("mujhe kal ke", True),
        ("New patient visit.", False),
        ("Can you do it again?", False),
        ("Yes.", False),
        ("okay ya", False),
    ],
)
def test_words_that_cannot_end_a_sentence(said: str, unfinished: bool) -> None:
    assert sounds_unfinished(said) is unfinished


@pytest.mark.asyncio
async def test_a_caller_who_pauses_mid_sentence_is_answered_once_with_all_of_it() -> None:
    stt = ScriptedSTT()
    heard: list[str] = []

    async def reply(result: TranscriptionResult) -> None:
        heard.append(result.text)

    pipeline = VoicePipeline(
        stream_sid="MZ" + "0" * 32,
        stt_provider=stt,
        tts_provider=TimedTTS(first_byte=0.0),
        send_frame=Line().send,
        on_transcript=reply,
    )
    await pipeline.start()
    try:
        stt.stream.say("I'd like to check my")
        await asyncio.sleep(0.3)
        stt.stream.say("appointment", final=False)
        await asyncio.sleep(0.3)
        stt.stream.say("appointment for tomorrow.")
        await asyncio.sleep(0.2)
        stt.stream.say("Thanks.")
        await asyncio.sleep(0.2)
    finally:
        await pipeline.close()
    # Before, "I'd like to check my" was answered on its own.
    assert heard == ["I'd like to check my appointment for tomorrow.", "Thanks."]


# --- A slow start at the model ---------------------------------------------------------


class SlowThenFast:
    """The first request takes long to start; any later one answers at once."""

    def __init__(self, first_delay: float) -> None:
        self.first_delay = first_delay
        self.requests = 0
        self.closed: list[int] = []

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.requests += 1
        number = self.requests
        try:
            if number == 1:
                await asyncio.sleep(self.first_delay)
            yield TextDelta(f"answer {number}")
            yield Completed.__new__(Completed)
        finally:
            self.closed.append(number)


@pytest.mark.asyncio
async def test_a_slow_start_gets_a_second_request_and_the_first_answer_wins() -> None:
    model = SlowThenFast(first_delay=5.0)
    gateway = ModelGateway(model, hedge_after=0.05)  # type: ignore[arg-type]
    started = asyncio.get_running_loop().time()
    events = [event async for event in gateway.stream(ChatRequest("", (), ()))]
    took = asyncio.get_running_loop().time() - started
    assert isinstance(events[0], TextDelta) and events[0].text == "answer 2"
    assert model.requests == 2 and 1 in model.closed and took < 1.0


@pytest.mark.asyncio
async def test_a_quick_start_sends_only_one_request() -> None:
    model = SlowThenFast(first_delay=0.0)
    gateway = ModelGateway(model, hedge_after=0.5)  # type: ignore[arg-type]
    events = [event async for event in gateway.stream(ChatRequest("", (), ()))]
    assert model.requests == 1 and isinstance(events[0], TextDelta)


# --- The hand-over ---------------------------------------------------------------------


def test_a_reply_starting_with_no_is_a_no() -> None:
    # Seen on a call: "do it" made this a yes, and a message was taken.
    assert consent("No. Can you do it do it again?", None) is False
    assert consent("नहीं, रहने दो", None) is False
    assert consent("Just do it", None) is True
    assert consent("Yes. Yes, please.", True) is True


@pytest.mark.asyncio
async def test_a_second_hand_over_lets_the_conversation_go_on(monkeypatch: Any) -> None:
    receptionist = receptionist_for(ScriptedEngine([]))
    monkeypatch.setattr(receptionist, "_ticket", AsyncMock())
    monkeypatch.setattr(receptionist, "_audit", AsyncMock())
    safety = receptionist.state.safety_state
    safety.transfer_attempted = True
    safety.decision = SafetyDecision.ESCALATE
    await receptionist._hand_over(AsyncMock(), Handoff("requested"))  # pyright: ignore[reportPrivateUsage]
    # Before, it stayed in hand-over and every later sentence got "I cannot connect you".
    assert safety.decision == SafetyDecision.CONTINUE
