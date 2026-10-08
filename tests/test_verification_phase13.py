from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.appointment_flow import AppointmentFlow, AppointmentOutput
from app.agents.runtime import AgentRuntime
from app.agents.tools import ToolRegistry
from app.calls.access import IdentifyPatient
from app.calls.verification import VerificationPolicy
from app.conversation.manager import ConversationStateManager
from app.conversation.redaction import redact_verification_secrets
from app.conversation.state import VerificationStatus
from app.conversation.store import MemoryConversationStateStore
from tests.test_agent_runtime import ScriptedProvider, context_and_state, decision


@pytest.mark.parametrize(
    "data",
    [
        {"required_factors": []},
        {"required_factors": ["date_of_birth"]},
        {"required_factors": ["reception_attestation", "reception_attestation"]},
        {"required_factors": ["reception_attestation", "sms_otp"]},
        {"max_code_attempts": 4},
        {"access_ttl_minutes": 0},
    ],
)
def test_verification_policy_rejects_unsafe_config(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VerificationPolicy.model_validate(data)


def test_verification_policy_accepts_json_factors() -> None:
    policy = VerificationPolicy.model_validate(
        {"required_factors": ["reception_attestation", "date_of_birth"]}
    )
    assert policy.model_dump(mode="json")["required_factors"] == [
        "reception_attestation",
        "date_of_birth",
    ]


def test_verification_requests_parse_iso_dates_without_exposing_them() -> None:
    from app.patients.schemas import UpdatePatientVerification

    command = UpdatePatientVerification.model_validate_json(
        '{"date_of_birth":"1990-04-09","account_identifier":"PAT-137"}'
    )
    assert command.date_of_birth is not None and command.date_of_birth.year == 1990


async def test_identity_state_survives_a_conversation_turn() -> None:
    call_id, tenant_id, patient_id = uuid4(), uuid4(), uuid4()
    state = ConversationStateManager.create_initial_state(call_id, tenant_id, "CAphase13")
    state.identity_state.record_attempt(True, patient_id)
    store = MemoryConversationStateStore()
    await store.save(state)
    restored = await store.get(tenant_id, call_id)
    assert restored is not None and restored.identity_state.is_verified
    ConversationStateManager.process_caller_utterance(restored, "I need my appointment")
    assert restored.identity_state.patient_id == patient_id


async def test_code_attempts_advance_explicit_verification_state() -> None:
    class Access:
        calls = 0

        async def identify(self, _context: object, _code: str):
            self.calls += 1
            return None if self.calls == 1 else patient_id

    patient_id = uuid4()
    context, state = context_and_state()
    access = Access()
    flow = AppointmentFlow(MagicMock(), access)  # type: ignore[arg-type]
    first = await flow.identify(context, state, IdentifyPatient(access_code="12345678"))
    assert AppointmentOutput.model_validate(first.model_dump()).status == "denied"
    assert state.identity_state.status == VerificationStatus.COLLECTING_IDENTIFIER
    assert state.identity_state.attempts == 1
    second = await flow.identify(context, state, IdentifyPatient(access_code="12345678"))
    assert AppointmentOutput.model_validate(second.model_dump()).status == "identified"
    assert state.identity_state.status == VerificationStatus.VERIFIED
    assert state.identity_state.patient_id == patient_id


def test_verification_attributes_are_not_patient_view_fields() -> None:
    from app.patients.schemas import PatientView

    assert "date_of_birth" not in PatientView.model_fields
    assert "account_identifier" not in PatientView.model_fields


def test_verification_material_is_redacted_before_storage_or_model_calls() -> None:
    text = "My access code is 12345678, DOB 1990-04-09 and account ID is PAT-137"
    cleaned = redact_verification_secrets(text)
    assert "12345678" not in cleaned
    assert "1990-04-09" not in cleaned
    assert "PAT-137" not in cleaned
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CAphase13")
    ConversationStateManager.process_caller_utterance(state, text)
    assert state.last_caller_utterance == cleaned


async def test_agent_model_does_not_receive_verification_material() -> None:
    context, state = context_and_state()
    provider = ScriptedProvider([decision()])
    runtime = AgentRuntime(provider, ToolRegistry([]))
    result = await runtime.respond(
        context, state, "My code is 12345678, DOB 1990-04-09, account ID is PAT-137"
    )
    assert result.status == "completed"
    content = "\n".join(message.content for message in provider.requests[0].messages)
    assert "12345678" not in content
    assert "1990-04-09" not in content
    assert "PAT-137" not in content
