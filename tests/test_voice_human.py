"""Talking like a person on a real call. The phrases are from a test call where the
receptionist misunderstood the caller."""

import json
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from app.ai.consent import ConsentReader, Reading
from app.ai.engine import Engine
from app.ai.identity import ContactIdentity
from app.ai.llm.base import LLMRequest
from app.ai.llm.openrouter import OpenRouterLLMProvider
from app.ai.speech_time import spoken_when
from app.ai.tools.appointments import AppointmentFlow
from app.ai.tools.toolbox import build_tools
from app.ai.wording import spoken
from app.conversation.backchannel import is_closing
from app.conversation.confirmation import confirmation_answer
from app.conversation.manager import ConversationStateManager
from app.conversation.state import AppointmentOperation
from app.safety.classifier import SafetySignal
from app.safety.engine import SafetyEngine
from app.voice.stt.base import TranscriptionResult
from tests.fake_llm import ScriptedModel
from tests.test_voice_latency import ScriptedEngine, receptionist_for, started_pipeline
from tests.test_voice_transfer import call, clinic
from tests.voice_sim import Line, TimedTTS


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("हां, कर दो ना, कोई बात नहीं.", True),  # "no problem" is agreement
        ("हां हां, book कर दो. कोई बात नहीं, चलेगा वह मुझे.", True),
        ("हां, कर दो कर दो वह booking कर दो मुझे वह चाहिए.", True),
        ("Hi. Yes. Uh-huh.", True),
        ("sounds good, go ahead", True),
        ("theek hai", True),
        ("Yes.", True),
        ("Yes. Combo.", None),  # a misheard word: the quicker model reads it
        ("yes but tomorrow", None),  # a change is never a yes
        ("ok what about tuesday", None),
        ("No, another time", False),
        ("मत करो", False),
        ("ना फिर से आप मुझे repeat कर दें", False),
        ("Yes! No!", False),
    ],
)
def test_natural_replies_to_shall_i_book(reply: str, expected: bool | None) -> None:
    assert confirmation_answer(reply, "hi") is expected


def test_times_are_said_the_way_people_say_them() -> None:
    sunday_night = datetime(2026, 10, 25, 17, 37, tzinfo=UTC)  # 23:07 in Kolkata

    def at(hour: int, minute: int, language: str) -> str:
        # Monday in Kolkata, given in UTC.
        moment = datetime(2026, 10, 26, hour, minute, tzinfo=UTC)
        return spoken_when(moment, "Asia/Kolkata", language, sunday_night)

    assert at(12, 0, "hi") == "कल शाम साढ़े पाँच बजे"
    assert at(12, 0, "hinglish") == "kal shaam saadhe paanch baje"
    assert at(12, 0, "en") == "tomorrow at 5:30 p.m."
    assert at(3, 45, "hi") == "कल सुबह सवा नौ बजे"
    assert at(4, 15, "hi") == "कल सुबह पौने दस बजे"
    assert at(8, 0, "hi") == "कल दोपहर डेढ़ बजे"
    assert at(12, 0, "gu") == "આવતીકાલે સાંજે સાડા પાંચ વાગ્યે"
    later = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)
    assert spoken_when(later, "Asia/Kolkata", "en", sunday_night) == (
        "on Thursday, November 5 at 10 a.m."
    )


def test_the_booking_question_sounds_like_a_receptionist() -> None:
    question = spoken("hi", "book_offer", time="कल शाम साढ़े पाँच बजे", provider="Dr. Rao")
    assert question == "तो कल शाम साढ़े पाँच बजे, Dr. Rao के साथ। क्या मैं यह बुक कर दूँ?"
    assert spoken("hi", "confirm", offer=question) == question  # no "पुष्टि के लिए हाँ कहें"


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("Nomás.", True),
        ("आप call call कर, cut कर सकते हो.", True),
        ("Ok. Bye-bye, sir. Thank you.", True),
        ("wait, one more thing", False),
        ("Can you also check Tuesday?", False),
    ],
)
def test_what_ends_the_call_after_the_goodbye(said: str, expected: bool) -> None:
    assert is_closing(said, "hi") is expected


async def test_a_bye_after_the_goodbye_hangs_up(monkeypatch: Any) -> None:
    hung_up = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.hang_up", hung_up)
    receptionist = receptionist_for(ScriptedEngine([]))
    pipeline = await started_pipeline(Line(), TimedTTS(first_byte=0.0))
    receptionist.pipeline = pipeline
    receptionist.state.end_requested = True
    try:
        await receptionist.reply(TranscriptionResult("Nomás.", True))
    finally:
        await pipeline.close()
    hung_up.assert_awaited_once()


class Human:
    """A second opinion that hears a request for a person in everything."""

    async def classify(self, utterance: str, context_hint: str) -> SafetySignal:
        return SafetySignal(kind="human", confidence=0.97, reason_code="human_request")


async def test_on_a_call_the_second_opinion_never_offers_a_transfer() -> None:
    _, state = call(clinic())
    message = await SafetyEngine(Human(), ask_first=True).evaluate(  # type: ignore[arg-type]
        state, "आप फिर से repeat कर सकते हैं क्या?"
    )
    assert message is None and not state.safety_state.transfer_offered


class Reader:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.requests: list[LLMRequest] = []

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        self.requests.append(request)
        return schema.model_validate(Reading(answer=self.answer).model_dump())  # type: ignore[arg-type]


class Flow:
    """Notes how the reply was read."""

    def __init__(self) -> None:
        self.answers: list[bool | None] = []

    async def before_turn(self, session: Any, state: Any, utterance: str, answer: Any) -> None:
        self.answers.append(answer)


async def test_an_unclear_yes_is_read_by_the_quicker_model_only_when_asked() -> None:
    reader = Reader("yes")
    flow = Flow()
    registry, _, _ = build_tools(MagicMock(), MagicMock(), None)
    engine = Engine(
        ScriptedModel("Sure.", "Sure."),
        registry,
        cast(AppointmentFlow, flow),
        SafetyEngine(ask_first=True),
        cast(ContactIdentity, None),
        consent=ConsentReader(reader),  # type: ignore[arg-type]
    )
    session, state = call(clinic())
    # Nothing was asked: the reply is not read at all.
    ConversationStateManager.process_caller_utterance(state, "Yes. Combo.")
    _ = [event async for event in engine.run(session, state, "Yes. Combo.")]
    assert flow.answers == [None] and reader.requests == []
    # A booking is waiting for a yes: the misheard reply is read as one.
    state.appointment_operation = AppointmentOperation(
        action="book_appointment", params={}, key=str(uuid4()), summary="Shall I book it?"
    )
    state.last_agent_response = "So that's tomorrow at five thirty with Dr. Rao. Shall I book it?"
    ConversationStateManager.process_caller_utterance(state, "Yes. Combo.")
    _ = [event async for event in engine.run(session, state, "Yes. Combo.")]
    assert flow.answers[-1] is True and len(reader.requests) == 1
    assert "Shall I book it?" in json.loads(reader.requests[0].messages[-1].content)["question"]


async def test_calls_think_little_and_take_the_quickest_endpoint() -> None:
    sent: list[dict[str, Any]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    from app.ai.llm.base import ChatRequest, UserTurn

    for fastest in (False, True):
        provider = OpenRouterLLMProvider(
            SecretStr("or"), "google/gemini-3.8-flash", 5, 100,
            reasoning_effort="minimal" if fastest else "low", http_client=client, fastest=fastest,
        )  # fmt: skip
        with pytest.raises(Exception):  # noqa: B017 - only the request body matters here
            _ = [event async for event in provider.stream(ChatRequest("s", (UserTurn("hi"),)))]
    await client.aclose()
    assert sent[0]["provider"]["sort"] == "price" and sent[0]["reasoning"]["effort"] == "low"
    assert sent[1]["provider"]["sort"] == "latency" and sent[1]["reasoning"]["effort"] == "minimal"
