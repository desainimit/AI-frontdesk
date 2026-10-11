from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel

from app.ai.engine import Done, Engine, Event, Handoff, Say, Worked, Working
from app.ai.identity import ContactIdentity
from app.ai.llm.base import LLMUnavailableError, ToolCall, ToolResult
from app.ai.session import Session
from app.ai.tools.appointments import AppointmentFlow, NoInput, Outcome
from app.ai.tools.registry import Tool, ToolRegistry
from app.ai.tools.toolbox import Listing, TransferInput
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, SafetyDecision
from app.safety.engine import SafetyEngine
from app.settings.defaults import default_settings
from tests.fake_llm import ScriptedModel, Step

CONFIRM = "Dr Rao. Book your appointment at Mon 12 Oct, 10:00 AM? Say yes to confirm."


class NoFlow:
    """Nothing is waiting for a yes or no."""

    async def before_turn(self, *_: Any) -> None:
        return None


def conversation(channel: str = "sms") -> tuple[Session, ConversationState]:
    settings = default_settings("Sunrise Clinic")
    tenant_id, conversation_id = uuid4(), uuid4()
    session = Session(
        tenant_id,
        conversation_id,
        cast(Any, channel),
        uuid4(),
        settings,
        settings.receptionist.allowed_tools,
    )
    state = ConversationStateManager.create_initial_state(conversation_id, tenant_id, "text")
    return session, state


def engine(*steps: Step) -> tuple[Engine, ScriptedModel]:
    async def providers(*_: Any) -> BaseModel:
        return Listing(items=[{"id": "p1", "name": "Dr Rao"}])

    async def book(*_: Any) -> BaseModel:
        return Outcome(status="confirmation_required", say=CONFIRM)

    async def transfer(_: Session, state: ConversationState, __: BaseModel) -> BaseModel:
        state.safety_state.decision = SafetyDecision.ESCALATE
        state.safety_state.triggered_at = datetime.now(UTC)
        return Outcome(status="handing_over")

    registry = ToolRegistry(
        [
            Tool("list_providers", "Doctors.", NoInput, providers),
            Tool("book_appointment", "Propose a booking.", NoInput, book),
            Tool("transfer_to_staff", "Ask for a person.", TransferInput, transfer),
        ]
    )
    model = ScriptedModel(*steps)
    built = Engine(
        model,
        registry,
        cast(AppointmentFlow, NoFlow()),
        SafetyEngine(),
        cast(ContactIdentity, None),
    )
    return built, model


async def turn(
    built: Engine, session: Session, state: ConversationState, said: str
) -> tuple[list[Event], Done]:
    ConversationStateManager.process_caller_utterance(state, said)
    events = [event async for event in built.run(session, state, said)]
    done = events[-1]
    assert isinstance(done, Done)
    return events, done


async def test_a_reply_comes_out_sentence_by_sentence() -> None:
    built, _ = engine("We are open from nine to six. Would you like to book a visit?")
    events, done = await turn(built, *conversation(), "When are you open?")
    assert [event.text for event in events if isinstance(event, Say)] == [
        "We are open from nine to six.",
        "Would you like to book a visit?",
    ]
    assert done.outcome == "answered" and done.first_text_ms is not None
    assert (done.usage.input_tokens, done.model) == (10, "scripted")


async def test_the_model_looks_something_up_and_answers_from_it() -> None:
    built, model = engine([ToolCall("c1", "list_providers", {})], "We have Dr Rao.")
    events, done = await turn(built, *conversation(), "Which doctors do you have?")
    assert [type(event) for event in events] == [Working, Worked, Say, Done]
    assert done.tools == ("list_providers",) and done.steps == 2
    result = model.requests[1].turns[-1]
    assert isinstance(result, ToolResult) and result.content["items"][0]["name"] == "Dr Rao"


async def test_a_proposal_is_put_to_the_person_in_the_servers_own_words() -> None:
    built, model = engine([ToolCall("c1", "book_appointment", {})])
    events, done = await turn(built, *conversation(), "Book me in on Monday at ten")
    assert [event.text for event in events if isinstance(event, Say)] == [CONFIRM]
    # The model is not asked to rephrase the question.
    assert done.outcome == "confirmation" and len(model.requests) == 1


async def test_arguments_the_model_got_wrong_come_back_to_it_to_fix() -> None:
    built, model = engine(
        [ToolCall("c1", "list_providers", {"surprise": True})],
        [ToolCall("c2", "list_providers", {})],
        "We have Dr Rao.",
    )
    events, done = await turn(built, *conversation(), "Which doctors do you have?")
    assert [event.ok for event in events if isinstance(event, Worked)] == [False, True]
    refused = model.requests[1].turns[-1]
    assert isinstance(refused, ToolResult) and refused.content == {"status": "invalid_arguments"}
    assert done.outcome == "answered"


async def test_a_model_that_is_down_ends_in_an_apology_and_a_hand_over() -> None:
    built, _ = engine(LLMUnavailableError("down"))
    session, state = conversation()
    events, done = await turn(built, session, state, "Hello")
    assert [type(event) for event in events] == [Say, Handoff, Done]
    assert cast(Handoff, events[1]).reason == "unavailable"
    assert (done.outcome, done.error) == ("unavailable", "llm_unavailable")
    assert "team" in done.reply


async def test_an_emergency_gets_the_fixed_script_and_never_reaches_the_model() -> None:
    built, model = engine("unused")
    session, state = conversation()
    events, done = await turn(built, session, state, "My father is not breathing")
    assert done.outcome == "safety" and model.requests == []
    assert any(isinstance(event, Handoff) and event.reason == "emergency" for event in events)
    assert state.safety_state.decision == SafetyDecision.EMERGENCY_PROTOCOL


async def test_asking_for_a_person_hands_the_conversation_over() -> None:
    built, _ = engine([ToolCall("c1", "transfer_to_staff", {})])
    events, done = await turn(built, *conversation(), "Can someone from the clinic help me?")
    assert done.outcome == "handoff"
    assert any(isinstance(event, Handoff) and event.reason == "requested" for event in events)
