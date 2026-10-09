import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.conversation.manager import ConversationStateManager
from app.conversation.state import (
    ConfirmationStatus,
    FlowStep,
    FlowType,
    IdentifierType,
    LanguageState,
    SafetyDecision,
    VerificationStatus,
)
from app.conversation.store import MemoryConversationStateStore, RedisConversationStateStore
from app.voice.pipeline import VoicePipeline
from app.voice.stt.base import TranscriptionResult
from tests.fake_stt import MockSTTProvider
from tests.fake_tts import MockTTSProvider


@pytest.mark.asyncio
async def test_conversation_initial_state() -> None:
    call_id = uuid4()
    tenant_id = uuid4()
    state = ConversationStateManager.create_initial_state(
        call_id=call_id,
        tenant_id=tenant_id,
        telephony_call_id="CA1234567890abcdef1234567890abcdef",
        caller_phone="+15551234567",
        default_language="en",
    )

    assert state.call_id == call_id
    assert state.tenant_id == tenant_id
    assert state.call_state.caller_phone == "+15551234567"
    assert state.language == "en"
    assert not state.awaiting_confirmation
    assert state.identity_state.status == VerificationStatus.UNVERIFIED
    assert state.active_flow_state.flow_type == FlowType.IDLE
    assert state.safety_state.decision == SafetyDecision.CONTINUE
    assert state.appointment_draft is None


def test_language_state_stability_and_code_switching() -> None:
    lang_state = LanguageState(primary_language="en")

    # Turn 1: Caller introduces Hindi
    lang_state.record_utterance("hi", confidence=0.8)
    assert lang_state.recent_languages[-1] == "hi"

    # Turn 2: Caller uses Hinglish (mixing Hindi and English terms)
    lang_state.record_utterance("hinglish", confidence=0.85)
    assert lang_state.code_switching is True

    # Turn 3: Caller uses a single English phrase ("Actually Monday is better")
    lang_state.record_utterance("en", confidence=0.6)
    # The session language remains stable; code_switching is still detected
    assert lang_state.code_switching is True

    # Turn 4 & 5: Sustained English switches primary language
    lang_state.record_utterance("en", confidence=0.85)
    lang_state.record_utterance("en", confidence=0.9)
    assert lang_state.primary_language == "en"


def test_identity_state_lifecycle() -> None:
    call_id = uuid4()
    tenant_id = uuid4()
    state = ConversationStateManager.create_initial_state(
        call_id=call_id,
        tenant_id=tenant_id,
        telephony_call_id="CAtest",
    )

    identity = state.identity_state
    assert not identity.is_verified
    assert identity.status == VerificationStatus.UNVERIFIED

    # Start collection
    identity.start_collecting(IdentifierType.PHONE)
    assert identity.status == VerificationStatus.COLLECTING_IDENTIFIER
    assert identity.identifier_type == IdentifierType.PHONE

    # Failed attempt 1
    assert not identity.record_attempt(success=False)
    assert identity.attempts == 1
    assert identity.status == VerificationStatus.COLLECTING_IDENTIFIER

    # Failed attempt 2 & 3 (max_attempts = 3)
    identity.record_attempt(success=False)
    identity.record_attempt(success=False)
    assert identity.attempts == 3
    assert identity.status == VerificationStatus.FAILED

    # Reset and test success
    identity.status = VerificationStatus.COLLECTING_IDENTIFIER
    identity.attempts = 0
    patient_id = uuid4()
    assert identity.record_attempt(success=True, patient_id=patient_id)
    assert identity.is_verified
    assert identity.patient_id == patient_id
    assert identity.verified_at is not None


def test_context_maintenance_across_multiple_utterances() -> None:
    call_id = uuid4()
    tenant_id = uuid4()
    state = ConversationStateManager.create_initial_state(
        call_id=call_id,
        tenant_id=tenant_id,
        telephony_call_id="CAtest",
    )

    # Turn 1: "I need to book an appointment"
    ConversationStateManager.process_caller_utterance(
        state, "I want to schedule an appointment with a doctor"
    )
    assert state.active_flow_state.flow_type == FlowType.APPOINTMENT_BOOKING
    assert state.active_flow_state.step == FlowStep.COLLECTING_DETAILS
    assert state.appointment_draft is not None

    # Turn 2: "Dr Shah" (Short context-dependent answer)
    ConversationStateManager.process_caller_utterance(state, "Dr Shah")
    assert state.active_flow_state.get_slot("provider_name") == "Dr Shah"
    assert state.appointment_draft.provider_name == "Dr Shah"

    # Turn 3: "Tomorrow" (Short context-dependent answer)
    ConversationStateManager.process_caller_utterance(state, "Tomorrow")
    assert state.active_flow_state.get_slot("date") == "tomorrow"

    # Turn 4: "Afternoon" (Short context-dependent answer)
    ConversationStateManager.process_caller_utterance(state, "In the afternoon")
    assert state.active_flow_state.get_slot("time_preference") == "afternoon"

    # Turn 5: Propose slot -> awaiting confirmation
    action_params: dict[str, Any] = {
        "provider_name": "Dr Shah",
        "date": "tomorrow",
        "slot_time": "2:00 PM",
    }
    ConversationStateManager.propose_appointment_slot(
        state,
        action_params=action_params,
        prompt="I found a slot with Dr Shah tomorrow at 2:00 PM. Would you like me to book it?",
    )
    assert state.awaiting_confirmation
    assert state.active_flow_state.step == FlowStep.AWAITING_CONFIRMATION

    # Turn 6: "Yes, book that"
    ConversationStateManager.process_caller_utterance(state, "Yes, book that please")
    assert state.confirmation_state.status == ConfirmationStatus.CONFIRMED
    assert not state.awaiting_confirmation
    assert state.active_flow_state.step == FlowStep.EXECUTING


def test_confirmation_rejection_and_expiration() -> None:
    call_id = uuid4()
    tenant_id = uuid4()
    state = ConversationStateManager.create_initial_state(
        call_id=call_id,
        tenant_id=tenant_id,
        telephony_call_id="CAtest",
    )

    ConversationStateManager.process_caller_utterance(state, "Book an appointment")
    ConversationStateManager.propose_appointment_slot(
        state,
        action_params={"slot": "10am"},
        prompt="Book at 10am?",
        timeout_seconds=120.0,  # Rejection is tested here; expiration is tested below.
    )

    # Negative response denies confirmation
    ConversationStateManager.process_caller_utterance(state, "No, that doesn't work")
    assert state.confirmation_state.status == ConfirmationStatus.DENIED
    assert state.active_flow_state.step == FlowStep.OFFERING_SLOTS

    # Test expiration
    state.confirmation_state.request("book", {"slot": "10am"}, "Confirm?", timeout_seconds=-10.0)
    assert not state.confirmation_state.is_awaiting
    with pytest.raises(ValueError, match="not awaiting confirmation or has expired"):
        state.confirmation_state.confirm()


@pytest.mark.asyncio
async def test_memory_conversation_store() -> None:
    store = MemoryConversationStateStore()
    call_id = uuid4()
    tenant_id = uuid4()
    state = ConversationStateManager.create_initial_state(
        call_id=call_id,
        tenant_id=tenant_id,
        telephony_call_id="CAtest",
    )

    # Initial state should not be in store
    assert await store.get(tenant_id, call_id) is None

    # Save and retrieve
    await store.save(state, ttl_seconds=3600)
    retrieved = await store.get(tenant_id, call_id)
    assert retrieved is not None
    assert retrieved.call_id == call_id
    assert retrieved.tenant_id == tenant_id

    # Tenant isolation: different tenant cannot access state
    assert await store.get(uuid4(), call_id) is None

    # Delete state
    await store.delete(tenant_id, call_id)
    assert await store.get(tenant_id, call_id) is None


@pytest.mark.asyncio
async def test_redis_conversation_store_mocked() -> None:
    fake_redis = AsyncMock()
    store = RedisConversationStateStore(fake_redis)

    call_id = uuid4()
    tenant_id = uuid4()
    state = ConversationStateManager.create_initial_state(
        call_id=call_id,
        tenant_id=tenant_id,
        telephony_call_id="CAtest",
    )

    # Save state
    await store.save(state, ttl_seconds=1800)
    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.call_args
    assert args[0] == f"tenant:{tenant_id}:call:{call_id}:conv_state"
    assert kwargs.get("ex") == 1800

    # Get state
    fake_redis.get.return_value = state.model_dump_json().encode("utf-8")
    retrieved = await store.get(tenant_id, call_id)
    assert retrieved is not None
    assert retrieved.call_id == call_id

    # Delete state
    await store.delete(tenant_id, call_id)
    fake_redis.delete.assert_awaited_once_with(f"tenant:{tenant_id}:call:{call_id}:conv_state")


@pytest.mark.asyncio
async def test_voice_pipeline_conversation_state_integration() -> None:
    expected_result = TranscriptionResult(
        text="I want to book an appointment with Dr Shah",
        is_final=True,
        confidence=0.95,
        language="en",
    )
    mock_stt = MockSTTProvider(name="mock_stt", results=[expected_result])
    mock_tts = MockTTSProvider(name="mock_tts")
    store = MemoryConversationStateStore()

    call_id = uuid4()
    tenant_id = uuid4()
    state = ConversationStateManager.create_initial_state(
        call_id=call_id,
        tenant_id=tenant_id,
        telephony_call_id="CApipeline",
    )
    await store.save(state)

    sent_frames: list[dict[str, Any]] = []

    async def send_frame(msg: dict[str, Any]) -> None:
        sent_frames.append(msg)

    pipeline = VoicePipeline(
        stream_sid="MZ123",
        stt_provider=mock_stt,
        tts_provider=mock_tts,
        send_frame=send_frame,
        conversation_state=state,
        conversation_store=store,
    )
    await pipeline.start()

    # Feed enough audio frames to trigger the mock STT stream to emit the result
    await pipeline.process_inbound_audio(b"\x00" * 320)

    # Yield to let consumer task process transcript
    await asyncio.sleep(0.05)

    # Verify conversation state was updated and saved
    assert state.utterance_count == 1
    assert state.last_caller_utterance == "I want to book an appointment with Dr Shah"
    assert state.active_flow_state.flow_type == FlowType.APPOINTMENT_BOOKING
    assert state.active_flow_state.get_slot("provider_name") == "Dr Shah"

    # Verify persisted in store
    persisted = await store.get(tenant_id, call_id)
    assert persisted is not None
    assert persisted.utterance_count == 1

    # Assistant speaks response
    await pipeline.speak("I can help you book with Dr Shah.")
    assert state.last_agent_response == "I can help you book with Dr Shah."

    await pipeline.close()
