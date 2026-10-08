from uuid import uuid4

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app.conversation.manager import ConversationStateManager
from app.conversation.state import TranscriptTurn
from app.conversation.store import encode
from app.postcall.schemas import PostCallInput, analyze_post_call
from app.workflows.definitions import PostCallWorkflow
from app.workflows.intent import WorkflowRequest


def test_transcript_capture_redacts_identifiers_and_stays_bounded() -> None:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CAtest")
    ConversationStateManager.process_caller_utterance(
        state,
        "Contact me at person@example.com or +1 (415) 555-2671; code 12345678",
    )
    first = state.transcript_turns[0].text
    assert "person@example.com" not in first
    assert "415" not in first
    assert "12345678" not in first
    assert "[email]" in first and "[phone number]" in first
    for index in range(85):
        state.append_transcript_turn("agent", f"Response {index}")
    snapshot = PostCallInput.capture(state)
    assert len(snapshot.turns) == 80
    assert snapshot.turns[-1].text == "Response 84"
    assert "person@example.com" not in snapshot.model_dump_json()
    for _ in range(80):
        state.append_transcript_turn("caller", "न" * 400)
    assert len(encode(state, 3600).encode()) <= 65536


def test_post_call_analysis_preserves_language_and_creates_structured_follow_up() -> None:
    snapshot = PostCallInput(
        language="es",
        intent="book_appointment",
        appointment_action="book_appointment",
        appointment_status="proposed",
        turns=[
            TranscriptTurn(
                speaker="caller", text="Necesito una cita, maria@example.com", language="es"
            ),
            TranscriptTurn(speaker="agent", text="Buscaré una cita.", language="es"),
        ],
    )
    analysis = analyze_post_call("completed", snapshot)
    assert "Necesito una cita" in analysis.summary
    assert "maria@example.com" not in analysis.summary
    assert analysis.outcome.language == "es"
    assert analysis.outcome.follow_up == "appointment_followup"
    assert analysis.outcome.resolution == "needs_follow_up"
    assert analysis.turn_count == 2
    assert analyze_post_call("completed", snapshot).transcript_digest == analysis.transcript_digest


def test_post_call_emergency_and_missing_snapshot_require_review() -> None:
    emergency = analyze_post_call("completed", PostCallInput(safety_decision="emergency_protocol"))
    assert emergency.outcome.follow_up == "human_review"
    absent = analyze_post_call("failed", None)
    assert absent.summary == "No transcript available."
    assert absent.outcome.follow_up == "human_review"


async def test_post_call_workflow_waits_for_late_snapshot() -> None:
    attempts = 0

    @activity.defn(name="record_post_call")
    async def record_post_call(_request: WorkflowRequest) -> str:
        nonlocal attempts
        attempts += 1
        return "pending" if attempts < 3 else "recorded"

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue="post-call-late-snapshot",
            workflows=[PostCallWorkflow],
            activities=[record_post_call],
        ),
    ):
        result = await environment.client.execute_workflow(
            PostCallWorkflow.run,
            WorkflowRequest(str(uuid4()), str(uuid4())),
            id="post-call-late-snapshot-" + uuid4().hex,
            task_queue="post-call-late-snapshot",
        )
        assert result == "recorded"
    assert attempts == 3
