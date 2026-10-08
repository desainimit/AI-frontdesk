from typing import cast
from uuid import uuid4

from app.conversation.manager import ConversationStateManager
from app.conversation.state import TranscriptTurn
from app.conversation.store import encode
from app.postcall.schemas import PostCallInput, analyze_post_call
from app.workflows.activities import WorkflowActivities
from app.workflows.intent import WorkflowRequest
from app.workflows.runner import JobRunner
from app.workflows.store import ClaimedIntent, WorkflowIntentStore


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


class ScriptedStore:
    """One queued intent whose attempts grow each time it is retried."""

    def __init__(self) -> None:
        self.queued, self.attempts = True, 0
        self.finished: list[tuple[str | None, bool]] = []

    async def claim(self) -> ClaimedIntent | None:
        if not self.queued:
            return None
        self.queued, self.attempts = False, self.attempts + 1
        request = WorkflowRequest(str(uuid4()), str(uuid4()))
        return ClaimedIntent(uuid4(), "post_call", request, uuid4(), self.attempts)

    async def finish(
        self, intent: ClaimedIntent, *, error: str | None = None, dead: bool = False
    ) -> bool:
        self.finished.append((error, dead))
        self.queued = error is not None and not dead
        return True


class ScriptedActivities:
    def __init__(self, outcomes: list[str | Exception]) -> None:
        self.outcomes = outcomes

    async def record_post_call(self, _request: WorkflowRequest) -> str:
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def runner(store: ScriptedStore, outcomes: list[str | Exception], attempts: int = 8) -> JobRunner:
    return JobRunner(
        cast(WorkflowIntentStore, store),
        cast(WorkflowActivities, ScriptedActivities(outcomes)),
        max_attempts=attempts,
    )


async def test_post_call_job_waits_for_late_snapshot_then_completes() -> None:
    store = ScriptedStore()
    jobs = runner(store, ["pending", "pending", "recorded"])
    assert [await jobs.process_one() for _ in range(4)] == [True, True, True, False]
    assert store.finished == [("pending", False), ("pending", False), (None, False)]


async def test_failing_job_is_retried_and_then_retired() -> None:
    store = ScriptedStore()
    jobs = runner(store, [RuntimeError("private detail"), ValueError("private detail")], 2)
    assert [await jobs.process_one() for _ in range(3)] == [True, True, False]
    # The second failure reaches the attempt limit, so it stops being retried.
    assert store.finished == [("failed", False), ("failed", True)]
