"""Listening for the end of a sentence, a slow model start, and the hand-over loop seen
on a real call. Everything runs against stand-ins; no provider is called."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors
from google.genai import types as genai_types
from pydantic import SecretStr

from app.ai.engine import Handoff, Say
from app.ai.llm.base import (
    ChatRequest,
    Completed,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelEvent,
    TextDelta,
)
from app.ai.llm.factory import TIMEOUT_SECONDS, open_models
from app.ai.llm.gateway import ModelGateway
from app.ai.llm.gemini import GeminiLLMProvider
from app.ai.sentences import SentenceSplitter
from app.conversation.backchannel import wants_to_end
from app.conversation.state import SafetyDecision
from app.conversation.turn_end import sounds_unfinished
from app.safety.handover import consent
from app.settings.defaults import default_settings
from app.voice.pipeline import VoicePipeline
from app.voice.stt.base import TranscriptionResult
from tests.test_voice_latency import ScriptedEngine, receptionist_for, started_pipeline
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


# --- When the model provider fails ---------------------------------------------------


class AlwaysDown:
    def __init__(self) -> None:
        self.requests = 0

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.requests += 1
        raise LLMUnavailableError("down")
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_on_a_call_a_failing_model_is_not_retried_in_a_ladder() -> None:
    primary, fallback = AlwaysDown(), AlwaysDown()
    gateway = ModelGateway(
        primary,  # type: ignore[arg-type]
        fallback=fallback,  # type: ignore[arg-type]
        pause_seconds=0,
        retries=0,
    )
    with pytest.raises(LLMUnavailableError):
        _ = [event async for event in gateway.stream(ChatRequest("", (), ()))]
    # Before, three tries of 10 s each kept a caller waiting 38 s.
    assert primary.requests == 1 and fallback.requests == 1


def test_calls_keep_the_timeout_google_accepts() -> None:
    # Gemini's SDK sends the timeout to Google as a deadline; 5 s was refused with
    # 400 INVALID_ARGUMENT on a real request. A call's limit is the gateway's instead.
    voice = open_models("gemini", SecretStr("k"), {}, "platform", voice=True).reply
    assert voice._http.timeout.read == TIMEOUT_SECONDS  # type: ignore[attr-defined]  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue, reportUnknownMemberType]


@pytest.mark.asyncio
async def test_on_a_call_a_model_that_does_not_start_answering_is_given_up_on() -> None:
    model = SlowThenFast(first_delay=5.0)
    gateway = ModelGateway(model, retries=0, first_words_within=0.1)  # type: ignore[arg-type]
    started = asyncio.get_running_loop().time()
    with pytest.raises(LLMTimeoutError):
        _ = [event async for event in gateway.stream(ChatRequest("", (), ()))]
    assert asyncio.get_running_loop().time() - started < 1.0
    # A model that answers in time is not affected.
    quick = ModelGateway(SlowThenFast(first_delay=0.0), first_words_within=0.5)  # type: ignore[arg-type]
    events = [event async for event in quick.stream(ChatRequest("", (), ()))]
    assert isinstance(events[0], TextDelta)


def test_a_gemini_failure_logs_googles_reason_but_not_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = GeminiLLMProvider(SecretStr("k"), "gemini-3.5-flash-lite", 5, 64)
    error = errors.APIError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "x"}})
    with caplog.at_level("WARNING"):
        mapped = provider._failed(error)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(mapped, LLMUnavailableError)
    record = next(item for item in caplog.records if item.msg == "gemini_request_failed")
    assert record.status == "UNAVAILABLE" and record.status_code == 503  # type: ignore[attr-defined]


# --- Goodbyes, first words, and the rest -------------------------------------------


@pytest.mark.parametrize(
    ("said", "last_reply", "ending"),
    [
        ("Ok. Thank you. You can make... Cut the call.", None, True),
        ("theek hai, phone rakhta hoon", None, True),
        ("No, thank you.", "Anything else I can help with?", True),
        ("Thank you so much", "Anything else I can help with?", True),
        ("Thank you so much", None, False),
        ("No, Wednesday is better", "Anything else?", False),
        ("No. Can you transfer the call?", "Anything else?", False),
        ("ok bye, but one more thing", None, False),
    ],
)
def test_a_goodbye_is_told_from_a_request(said: str, last_reply: str | None, ending: bool) -> None:
    assert wants_to_end(said, last_reply) is ending


@pytest.mark.asyncio
async def test_a_goodbye_is_said_warmly_at_once_and_the_call_ends(monkeypatch: Any) -> None:
    receptionist = receptionist_for(ScriptedEngine([(5.0, Say("never asked"))]))
    receptionist.session = MagicMock(configuration=default_settings("Sunrise Clinic"))
    receptionist.state.caller_name = "Nimit"
    hang_up = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.hang_up", hang_up)
    line, tts = Line(), TimedTTS(first_byte=0.0)
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(
            TranscriptionResult("Okay, thank you. You can cut the call.", True)
        )
    finally:
        await pipeline.close()
    assert tts.requests == [
        "Thanks so much for calling Sunrise Clinic, Nimit. Take care, and have a lovely day!"
    ]
    hang_up.assert_awaited_once()


def test_a_long_first_sentence_starts_at_its_first_comma_on_a_call() -> None:
    splitter = SentenceSplitter(first_clause=True)
    said: list[str] = []
    for piece in ("I've got Tuesday at ten ", "or Thursday at two thirty, ", "if either works. "):
        said += splitter.feed(piece)
    assert said == ["I've got Tuesday at ten or Thursday at two thirty,", "if either works."]
    # Texts keep whole sentences.
    whole = SentenceSplitter()
    assert whole.feed("I've got Tuesday at ten or Thursday at two thirty, if either works. ") == [
        "I've got Tuesday at ten or Thursday at two thirty, if either works."
    ]


def test_calls_ask_gemini_for_its_priority_lane() -> None:
    voice = open_models("gemini", SecretStr("k"), {}, "platform", voice=True).reply
    text = open_models("gemini", SecretStr("k"), {}, "platform", voice=False).reply
    assert voice._tier == genai_types.ServiceTier.PRIORITY  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    assert text._tier is None  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


@pytest.mark.asyncio
async def test_on_a_call_the_lighter_model_gets_what_is_left_not_a_fresh_wait() -> None:
    # On 27 Oct a slow provider kept a caller in silence 8 s, then 8 s more.
    gateway = ModelGateway(
        SlowThenFast(first_delay=5.0),  # type: ignore[arg-type]
        fallback=SlowThenFast(first_delay=5.0),  # type: ignore[arg-type]
        pause_seconds=0,
        retries=0,
        first_words_within=0.3,
    )
    started = asyncio.get_running_loop().time()
    with pytest.raises(LLMTimeoutError):
        _ = [event async for event in gateway.stream(ChatRequest("", (), ()))]
    assert asyncio.get_running_loop().time() - started < 0.45
