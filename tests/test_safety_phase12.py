"""Regression coverage for emergency overrides, context, and human transfer."""

import asyncio
from typing import cast
from urllib.parse import parse_qs
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from app.agents.appointment_flow import AppointmentFlow
from app.agents.context import AgentContext
from app.agents.providers.base import LLMRequest, LLMResponse
from app.agents.runtime import AgentRuntime
from app.agents.schemas import Intent
from app.agents.tools import ToolRegistry
from app.calls.service import CallContext
from app.config.settings import Settings
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConfirmationStatus, ConversationState, SafetyDecision
from app.safety.classifier import SafetySignal
from app.safety.engine import SafetyEngine
from app.safety.normalization import normalize_transcript
from app.safety.rules import deterministic_rule
from app.safety.transfer import TransferError, transfer_call, transfer_twiml
from app.tenants.schemas import TransferDestination


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse("unused")

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        self.calls += 1
        raise AssertionError("The normal LLM must not run")

    async def close(self) -> None:
        pass


def bound_state() -> tuple[AgentContext, ConversationState]:
    call = CallContext(uuid4(), uuid4(), "CA" + uuid4().hex, uuid4())
    state = ConversationStateManager.create_initial_state(
        call.call_id, call.tenant_id, call.provider_call_id
    )
    return AgentContext(call), state


@pytest.mark.parametrize(
    "phrase",
    [
        "I can't breathe.",
        "I think he's having a heart attack.",
        "I took too many tablets.",
        "My father collapsed",
        "He is unconscious",
        "He is seizing",
        "The bleeding will not stop",
        "I want to hurt myself",
        "I have chest pressure",
        "I cannot breathe since yesterday",
        "Since yesterday I have chest pain",
        "I had chest pain last year but now cannot breathe",
        "I had chest pain yesterday and today my chest hurts",
        "I have chest pain and a history of heart attack",
        "My chest hurts",
        "I have trouble breathing",
        "I am short of breath",
        "His face is drooping",
        "Saans nahi le pa raha",
        "Chest bahut tight lag raha hai.",
        "Saans nahi aa rahi.",
        "Mujhe seene mein dard hai.",
        "मुझे सांस नहीं आ रही",
        "सीने में दर्द हो रहा है",
        "No puedo respirar",
        "Je ne peux pas respirer",
        "Ich kann nicht atmen",
        "Não consigo respirar",
    ],
)
def test_clear_emergencies_are_deterministic(phrase: str) -> None:
    assert deterministic_rule(phrase).decision == SafetyDecision.EMERGENCY_PROTOCOL


@pytest.mark.parametrize(
    "phrase",
    [
        "No chest pain",
        "I do not have chest pain",
        "I do not have trouble breathing",
        "I had chest pain last year",
        "My father had a heart attack last year",
        "I have a history of heart attacks",
        "I had chest pain yesterday but now feel fine",
        "What is a heart attack?",
        "I need an emergency appointment",
        "Please book a chest X-ray appointment",
        "I am not having a heart attack",
        "My appointment is tomorrow",
    ],
)
def test_historical_negated_and_scheduling_phrases_do_not_trigger(phrase: str) -> None:
    assert deterministic_rule(phrase).decision == SafetyDecision.CONTINUE


def test_normalization_handles_speech_punctuation_and_zero_width() -> None:
    assert normalize_transcript("I CAN\u2019T\u200b breathe!") == "i cannot breathe"
    assert normalize_transcript("  साँस   नहीं आ रही  ") == "साँस नहीं आ रही"


async def test_emergency_preempts_llm_and_cancels_pending_mutation() -> None:
    context, state = bound_state()
    state.confirmation_state.request("book_appointment", {"slot_id": "old"}, "Book?", 120)
    provider = CountingProvider()
    runtime = AgentRuntime(provider, ToolRegistry([]))
    result = await runtime.respond(context, state, "I can't breathe")
    assert result.intent == Intent.EMERGENCY and result.status == "blocked"
    assert "emergency services" in result.response
    assert provider.calls == 0
    assert state.safety_state.decision == SafetyDecision.EMERGENCY_PROTOCOL
    assert state.confirmation_state.status == ConfirmationStatus.IDLE
    assert state.appointment_operation is None
    persisted = ConversationState.model_validate_json(state.model_dump_json())
    assert persisted.safety_state.is_emergency
    assert (await runtime.respond(context, persisted, "Hello")).intent == Intent.EMERGENCY
    assert provider.calls == 0


async def test_emergency_blocks_pending_appointment_commit_path() -> None:
    class AppointmentSpy:
        calls = 0

        async def before_turn(
            self, context: AgentContext, state: ConversationState, utterance: str
        ) -> None:
            self.calls += 1
            raise AssertionError("Appointment flow ran before safety")

    context, state = bound_state()
    state.confirmation_state.request("book_appointment", {"slot_id": "old"}, "Book?", 120)
    flow = AppointmentSpy()
    result = await AgentRuntime(
        CountingProvider(), ToolRegistry([]), appointment_flow=cast(AppointmentFlow, flow)
    ).respond(context, state, "I cannot breathe")
    assert result.intent == Intent.EMERGENCY
    assert flow.calls == 0
    assert state.confirmation_state.status == ConfirmationStatus.IDLE


async def test_ambiguous_context_then_worsening_preempts_agent() -> None:
    context, state = bound_state()
    provider = CountingProvider()
    runtime = AgentRuntime(provider, ToolRegistry([]))
    first = await runtime.respond(context, state, "My chest feels strange")
    assert first.status == "blocked" and state.safety_state.decision == SafetyDecision.CLARIFY
    second = await runtime.respond(context, state, "It is getting worse")
    assert second.intent == Intent.EMERGENCY
    assert provider.calls == 0


async def test_semantic_alarm_adds_to_rules_but_cannot_veto_clear_emergency() -> None:
    class Classifier:
        def __init__(self, kind: str) -> None:
            self.kind = kind

        async def classify(self, utterance: str, context_hint: str) -> SafetySignal:
            return SafetySignal.model_validate(
                {"kind": self.kind, "confidence": 0.98, "reason_code": "medical"}
            )

    _, state = bound_state()
    engine = SafetyEngine(Classifier("emergency"))  # type: ignore[arg-type]
    assert await engine.evaluate(state, "He suddenly cannot move one side") is not None
    assert state.safety_state.decision == SafetyDecision.EMERGENCY_PROTOCOL
    _, state2 = bound_state()
    engine2 = SafetyEngine(Classifier("none"))  # type: ignore[arg-type]
    await engine2.evaluate(state2, "I cannot breathe")
    assert state2.safety_state.decision == SafetyDecision.EMERGENCY_PROTOCOL


async def test_classifier_timeout_preserves_ambiguous_rule() -> None:
    class SlowClassifier:
        async def classify(self, utterance: str, context_hint: str) -> SafetySignal:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    _, state = bound_state()
    engine = SafetyEngine(SlowClassifier(), classifier_timeout_seconds=0.01)
    await engine.evaluate(state, "My chest feels strange")
    assert state.safety_state.decision == SafetyDecision.CLARIFY


async def test_access_code_is_not_sent_to_semantic_classifier() -> None:
    class SpyClassifier:
        calls = 0

        async def classify(self, utterance: str, context_hint: str) -> SafetySignal:
            self.calls += 1
            raise AssertionError("One-time access code was sent to classifier")

    _, state = bound_state()
    classifier = SpyClassifier()
    assert await SafetyEngine(classifier).evaluate(state, "12345678") is None
    assert classifier.calls == 0


async def test_human_escalation_is_honest_without_destination() -> None:
    _, state = bound_state()
    unavailable = SafetyEngine(transfer_available=False)
    message = await unavailable.evaluate(state, "Please transfer me to a human")
    assert state.safety_state.decision == SafetyDecision.ESCALATE
    assert message is not None and "cannot connect" in message
    _, state2 = bound_state()
    available = SafetyEngine(transfer_available=True)
    message2 = await available.evaluate(state2, "Please transfer me to a human")
    assert state2.safety_state.decision == SafetyDecision.TRANSFER
    assert message2 is not None and "connect you" in message2


async def test_twilio_transfer_updates_only_bound_call(settings: Settings) -> None:
    config = settings.model_copy(
        update={
            "twilio_account_sid": "AC" + "1" * 32,
            "twilio_auth_token": SecretStr("test-only-token"),
        }
    )
    context = CallContext(uuid4(), uuid4(), "CA" + "2" * 32, uuid4())
    captured: list[httpx.Request] = []

    def response(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"sid": context.provider_call_id})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        await transfer_call(config, context, "+15551234567", client=client)
    assert len(captured) == 1
    assert captured[0].url == (
        "https://api.twilio.com/2010-04-01/Accounts/"
        + config.twilio_account_sid
        + "/Calls/"
        + context.provider_call_id
        + ".json"
    )
    form = parse_qs(captured[0].content.decode())
    xml = fromstring(form["Twiml"][0])
    assert xml.findtext("Dial/Number") == "+15551234567"
    assert captured[0].headers["authorization"].startswith("Basic ")
    with pytest.raises(ValueError):
        transfer_twiml("https://malicious.example")
    with pytest.raises(ValueError):
        TransferDestination(number="12345")


async def test_twilio_transfer_failure_is_explicit(settings: Settings) -> None:
    config = settings.model_copy(update={"twilio_account_sid": "AC" + "1" * 32})
    context = CallContext(uuid4(), uuid4(), "CA" + "2" * 32, uuid4())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as client:
        with pytest.raises(TransferError):
            await transfer_call(config, context, "+15551234567", client=client)
