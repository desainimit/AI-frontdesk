"""Transfers on a call: the receptionist asks first, then puts the caller through."""

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import pytest
from pydantic import ValidationError

from app.ai.catalog import VOICE_TOOLS
from app.ai.engine import Done, Engine, Event, Handoff, Say
from app.ai.identity import ContactIdentity
from app.ai.llm.base import LLMUnavailableError, ToolCall
from app.ai.session import Session
from app.ai.tools.appointments import AppointmentFlow
from app.ai.tools.toolbox import build_tools
from app.api.telephony import whisper_line
from app.calls.service import CallContext
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, SafetyDecision
from app.core.settings import Settings
from app.safety.engine import SafetyEngine
from app.safety.handover_messages import handover_message
from app.safety.transfer import transfer_twiml
from app.settings.defaults import default_settings
from app.settings.schemas import CallSettings, ClinicConfiguration
from app.voice.twilio import twiml
from tests.fake_llm import ScriptedModel, Step
from tests.test_engine import NoFlow
from tests.test_telephony import voice_settings
from tests.test_voice_latency import ScriptedEngine, receptionist_for

FRONT = {"name": "the front desk", "number": "+919800000001"}
BILLING = {"name": "Billing", "number": "+919800000002", "description": "payments and bills"}


def clinic(*targets: dict[str, str], after_hours: str = "transfer") -> ClinicConfiguration:
    settings = default_settings("Lotus Clinic")
    return settings.model_copy(
        update={
            "calls": CallSettings.model_validate({"transfer_targets": list(targets)}),
            "hours": settings.hours.model_copy(update={"after_hours": after_hours}),
        }
    )


def call(configuration: ClinicConfiguration) -> tuple[Session, ConversationState]:
    tenant_id, call_id = uuid4(), uuid4()
    session = Session(
        tenant_id,
        call_id,
        "voice",
        uuid4(),
        configuration,
        configuration.receptionist.allowed_tools | VOICE_TOOLS,
        provider_call_id="CA" + "0" * 32,
    )
    state = ConversationStateManager.create_initial_state(call_id, tenant_id, "CA" + "0" * 32)
    return session, state


def voice_engine(*steps: Step) -> Engine:
    registry, _, _ = build_tools(MagicMock(), MagicMock(), None)
    return Engine(
        ScriptedModel(*steps),
        registry,
        cast(AppointmentFlow, NoFlow()),
        SafetyEngine(ask_first=True),
        cast(ContactIdentity, None),
    )


async def turn(
    built: Engine, session: Session, state: ConversationState, said: str
) -> tuple[list[str], list[Event], Done]:
    ConversationStateManager.process_caller_utterance(state, said)
    events = [event async for event in built.run(session, state, said)]
    done = events[-1]
    assert isinstance(done, Done)
    return [event.text for event in events if isinstance(event, Say)], events, done


def handed_over(events: list[Event]) -> bool:
    return any(isinstance(event, Handoff) for event in events)


async def test_asking_for_a_person_gets_an_offer_then_a_transfer_on_yes() -> None:
    built = voice_engine()
    session, state = call(clinic(FRONT))
    said, events, _ = await turn(built, session, state, "Can I talk to a person please?")
    assert said == [handover_message("en", "offer_first")] and not handed_over(events)
    said, events, done = await turn(built, session, state, "Yes")
    assert said == [handover_message("en", "connecting")]
    assert handed_over(events) and done.outcome == "handoff"
    assert state.safety_state.decision == SafetyDecision.TRANSFER


async def test_no_thanks_keeps_the_conversation_going() -> None:
    built = voice_engine()
    session, state = call(clinic(FRONT))
    await turn(built, session, state, "Let me speak to the receptionist")
    said, events, _ = await turn(built, session, state, "No")
    assert said == [handover_message("en", "declined")] and not handed_over(events)
    assert not state.safety_state.transfer_offered
    assert state.safety_state.decision == SafetyDecision.CONTINUE


async def test_asking_twice_puts_the_caller_through_without_arguing() -> None:
    built = voice_engine("We are open from nine to six.", "You're welcome.")
    session, state = call(clinic(FRONT))
    await turn(built, session, state, "I want to talk to a human")
    await turn(built, session, state, "What are your hours?")
    await turn(built, session, state, "Thanks")
    assert not state.safety_state.transfer_offered or state.utterance_count == 3
    # Long after the offer lapsed...
    said, events, _ = await turn(built, session, state, "Just connect me to a person")
    # ...but a second request is a request, not something to talk them out of.
    assert said == [handover_message("en", "connecting")] and handed_over(events)


async def test_the_model_offers_a_named_target_and_transfers_once_agreed() -> None:
    built = voice_engine([ToolCall("c1", "transfer_to_staff", {"target": "Billing"})])
    session, state = call(clinic(FRONT, BILLING))
    said, events, _ = await turn(built, session, state, "I have a question about my bill")
    assert said == [handover_message("en", "offer", target="Billing")]
    assert not handed_over(events)
    said, events, _ = await turn(built, session, state, "Sure, go ahead")
    assert handed_over(events) and state.safety_state.transfer_target == "Billing"


async def test_with_nobody_to_take_the_call_a_message_is_offered() -> None:
    built = voice_engine()
    # Closed, and the clinic does not transfer after hours: nobody to put through to.
    configuration = clinic(FRONT, after_hours="answer_only").model_copy(
        update={"hours": default_settings("x").hours.model_copy(update={"windows": []})}
    )
    session, state = call(configuration)
    said, _, _ = await turn(built, session, state, "Can I talk to a person?")
    assert said == [handover_message("en", "offer_message")]
    said, events, _ = await turn(built, session, state, "Yes please")
    assert said == [handover_message("en", "message_taken")] and handed_over(events)
    assert state.safety_state.decision == SafetyDecision.ESCALATE


async def test_when_the_model_fails_the_caller_is_asked_not_moved() -> None:
    down = LLMUnavailableError("down")
    built = voice_engine(down, down, down)
    session, state = call(clinic(FRONT))
    said, events, done = await turn(built, session, state, "Do you do blood tests?")
    assert said == [handover_message("en", "trouble_offer")]
    assert not handed_over(events) and done.outcome == "unavailable"


def test_saved_single_numbers_still_load_and_names_must_differ() -> None:
    legacy = CallSettings.model_validate({"transfer_number": "+919800000001"})
    assert [target.name for target in legacy.transfer_targets] == ["the front desk"]
    with pytest.raises(ValidationError):
        CallSettings.model_validate(
            {"transfer_targets": [FRONT, {**BILLING, "name": "THE FRONT DESK"}]}
        )


def test_targets_follow_the_clock() -> None:
    on_call = {"name": "On-call doctor", "number": "+919800000003", "when": "closed"}
    desk = {**FRONT, "when": "open"}
    configuration = clinic(desk, on_call)
    tuesday_noon = datetime(2026, 10, 27, 6, 30, tzinfo=UTC)  # 12:00 in Kolkata
    tuesday_night = datetime(2026, 10, 27, 17, 30, tzinfo=UTC)  # 23:00 in Kolkata
    assert [t.name for t in configuration.transfer_targets_now(tuesday_noon)] == ["the front desk"]
    assert [t.name for t in configuration.transfer_targets_now(tuesday_night)] == ["On-call doctor"]
    closed_for_transfers = clinic(desk, on_call, after_hours="callback")
    assert closed_for_transfers.transfer_targets_now(tuesday_night) == []


def test_warm_transfer_rings_with_a_whisper_and_comes_back_if_unanswered() -> None:
    xml = fromstring(
        transfer_twiml(
            "+919800000001",
            whisper_url="https://voice.example.test/w",
            action_url="https://voice.example.test/r",
        )
    )
    dial = xml.find("Dial")
    assert dial is not None and dial.attrib["answerOnBridge"] == "true"
    assert dial.attrib["action"] == "https://voice.example.test/r"
    number = dial.find("Number")
    assert number is not None and number.attrib["url"] == "https://voice.example.test/w"
    # Nothing is said after the dial: an unanswered transfer comes back instead.
    assert xml.find("Say") is None and xml.find("Hangup") is None


def test_staff_hear_who_is_calling_and_why(settings: Settings) -> None:
    _, state = call(clinic(FRONT))
    state.safety_state.transfer_target = "Billing"
    state.safety_state.handoff_note = "I was charged twice for my visit"
    assert whisper_line(state) == (
        "A caller from the receptionist for Billing. They said: I was charged twice for my "
        "visit. Connecting now."
    )
    state.safety_state.handoff_note = "मुझे बिल के बारे में पूछना है"
    assert "Their request is in the ticket." in whisper_line(state)
    config = voice_settings(settings)
    context = CallContext(uuid4(), uuid4(), "CA" + "2" * 32, uuid4())
    parameters = fromstring(twiml(config, context, resume=True)).findall("Connect/Stream/Parameter")
    assert {"name": "resume", "value": "1"} in [p.attrib for p in parameters]


@pytest.mark.asyncio
async def test_the_receptionist_puts_the_call_through_to_the_chosen_target(
    monkeypatch: Any,
) -> None:
    transfer = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.transfer_call", transfer)
    receptionist = receptionist_for(ScriptedEngine([]))
    session, state = call(clinic(FRONT, BILLING))
    receptionist.session, receptionist.state = session, state
    receptionist.settings = voice_settings(Settings())  # type: ignore[call-arg]
    receptionist._ticket = AsyncMock()  # type: ignore[method-assign]
    receptionist._audit = AsyncMock()  # type: ignore[method-assign]
    state.safety_state.decision = SafetyDecision.TRANSFER
    state.safety_state.transfer_target = "Billing"
    await receptionist._hand_over(MagicMock(), Handoff("requested"))  # pyright: ignore[reportPrivateUsage]
    awaited = transfer.await_args
    assert awaited is not None
    number, hooks = awaited.args[2], awaited.kwargs
    assert number == "+919800000002"
    assert hooks["whisper_url"].endswith(f"/{session.conversation_id}/whisper")
    assert hooks["action_url"].endswith(f"/{session.conversation_id}/transfer-result")
    assert receptionist.transferred and state.safety_state.transfer_confirmed


@pytest.mark.asyncio
async def test_a_message_for_the_team_lets_the_call_go_on(monkeypatch: Any) -> None:
    transfer = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.transfer_call", transfer)
    receptionist = receptionist_for(ScriptedEngine([]))
    session, state = call(clinic())
    receptionist.session, receptionist.state = session, state
    receptionist._ticket = AsyncMock()  # type: ignore[method-assign]
    receptionist._audit = AsyncMock()  # type: ignore[method-assign]
    state.safety_state.decision = SafetyDecision.ESCALATE
    await receptionist._hand_over(MagicMock(), Handoff("requested"))  # pyright: ignore[reportPrivateUsage]
    transfer.assert_not_awaited()
    receptionist._ticket.assert_awaited_once()  # type: ignore[attr-defined]
    assert state.safety_state.decision == SafetyDecision.CONTINUE
