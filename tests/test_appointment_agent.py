import json
from unittest.mock import MagicMock

import pytest

from app.agents.appointment_flow import affirmative
from app.agents.appointment_tools import appointment_tools
from app.agents.context import ALL_TOOLS, AgentContext
from app.agents.runtime import AgentRuntime
from app.agents.schemas import ToolCall
from app.agents.tools import ToolValidationError
from tests.test_agent_runtime import ScriptedProvider, context_and_state, decision


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("identify_patient", {"access_code": 12345678}),
        ("identify_patient", {"access_code": "123", "verified": True}),
        ("book_appointment", {"provider_id": "not-a-uuid", "starts_at": "tomorrow"}),
        (
            "book_appointment",
            {
                "provider_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "starts_at": "2030-01-01T10:00:00",
            },
        ),
        (
            "book_appointment",
            {
                "provider_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "starts_at": "2030-01-01T10:00:00Z",
                "confirmed": True,
            },
        ),
        (
            "cancel_appointment",
            {
                "appointment_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "patient_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            },
        ),
        (
            "reschedule_appointment",
            {
                "appointment_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "starts_at": "2030-01-01T10:00:00Z",
                "expected_version": 1,
            },
        ),
    ],
)
def test_appointment_arguments_cannot_inject_authority(name: str, arguments: dict[str, object]):
    context, state = context_and_state()
    registry, _ = appointment_tools(MagicMock())
    with pytest.raises(ToolValidationError):
        registry.validate(
            AgentContext(context.call, ALL_TOOLS),
            state,
            ToolCall(name=name, arguments_json=json.dumps(arguments)),
        )


def test_json_uuid_and_aware_datetime_are_accepted():
    context, state = context_and_state()
    registry, _ = appointment_tools(MagicMock())
    _, payload = registry.validate(
        AgentContext(context.call, ALL_TOOLS),
        state,
        ToolCall(
            name="book_appointment",
            arguments_json=json.dumps(
                {
                    "provider_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                    "starts_at": "2030-01-01T10:00:00+05:30",
                }
            ),
        ),
    )
    assert payload.model_dump(mode="json")["starts_at"] == "2030-01-01T10:00:00+05:30"


async def test_mutation_batch_is_rejected_before_any_handler_runs():
    context, state = context_and_state()
    sessions = MagicMock()
    registry, flow = appointment_tools(sessions)
    batch = decision(
        "cancel_appointment", '{"appointment_id":"3fa85f64-5717-4562-b3fc-2c963f66afa6"}'
    )
    batch["tool_calls"].append(batch["tool_calls"][0])
    agent = AgentRuntime(ScriptedProvider([batch]), registry, appointment_flow=flow)
    result = await agent.respond(AgentContext(context.call, ALL_TOOLS), state, "Cancel both")
    assert result.status == "unavailable"
    sessions.assert_not_called()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", True),
        ("Yes, book that please", True),
        ("no", False),
        ("yes but tomorrow", False),
        ("yes don't book", False),
        ("not yes", False),
        ("confirm=false", False),
    ],
)
def test_confirmation_is_explicit_and_negation_wins(text: str, expected: bool):
    assert affirmative(text) is expected
