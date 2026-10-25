"""Bounded snapshot and deterministic structured post-call outcome."""

from datetime import date, datetime
from hashlib import sha256
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.conversation.redaction import redact_transcript
from app.conversation.state import ConversationState, TranscriptTurn

FollowUpKind = Literal["human_review", "appointment_followup"]
CallOutcomeStatus = Literal["completed", "disconnected", "failed"]


def _empty_turns() -> list[TranscriptTurn]:
    return []


class PostCallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turns: list[TranscriptTurn] = Field(default_factory=_empty_turns, max_length=80)
    language: str = Field(default="en", min_length=2, max_length=24)
    intent: str | None = Field(default=None, max_length=40)
    safety_decision: str = Field(default="continue", max_length=40)
    appointment_action: str | None = Field(default=None, max_length=40)
    appointment_status: str | None = Field(default=None, max_length=40)
    # Written by the model at the end of the call; the rule-based summary is the fallback.
    summary: str | None = Field(default=None, max_length=1200)

    @classmethod
    def capture(cls, state: ConversationState, summary: str | None = None) -> "PostCallInput":
        operation = state.appointment_operation
        return cls(
            turns=state.transcript_turns[-80:],
            summary=summary[:1200] if summary else None,
            language=state.language,
            intent=state.active_intent,
            safety_decision=state.safety_state.decision.value,
            appointment_action=operation.action if operation else None,
            appointment_status=operation.status if operation else None,
        )


class StructuredOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_status: CallOutcomeStatus
    resolution: Literal["completed", "transferred", "needs_follow_up", "unavailable"]
    intent: str | None
    language: str
    safety_decision: str
    appointment_action: str | None
    follow_up: FollowUpKind | None


class PostCallAnalysis(BaseModel):
    summary: str = Field(max_length=1200)
    outcome: StructuredOutcome
    transcript_digest: str = Field(min_length=64, max_length=64)
    turn_count: int = Field(ge=0, le=80)


class PostCallReportView(PostCallAnalysis):
    model_config = ConfigDict(from_attributes=True)

    call_id: UUID
    call_ended_at: datetime
    created_at: datetime


class DailyCallAnalytics(BaseModel):
    day: date
    total: int
    completed: int
    needs_follow_up: int


def analyze_post_call(status: str, snapshot: PostCallInput | None) -> PostCallAnalysis:
    """Extract only observed speech and explicit state; never infer clinical facts."""
    call_status: CallOutcomeStatus = (
        cast(CallOutcomeStatus, status)
        if status in {"completed", "disconnected", "failed"}
        else "failed"
    )
    state = snapshot or PostCallInput()
    turns = [
        TranscriptTurn(
            speaker=turn.speaker,
            text=redact_transcript(turn.text)[:400],
            language=turn.language,
        )
        for turn in state.turns
    ]
    callers = [turn.text for turn in turns if turn.speaker == "caller"]
    agents = [turn.text for turn in turns if turn.speaker == "agent"]
    excerpts: list[str] = []
    if callers:
        excerpts.append(f"Caller: {callers[0]}")
        if len(callers) > 1 and callers[-1] != callers[0]:
            excerpts.append(f"Later: {callers[-1]}")
    if agents:
        excerpts.append(f"Final response: {agents[-1]}")
    summary = (
        redact_transcript(state.summary)[:1200]
        if state.summary
        else ". ".join(excerpts)[:1200] or "No transcript available."
    )
    if state.safety_decision in {"emergency_protocol", "escalate", "transfer"}:
        follow_up: FollowUpKind | None = "human_review"
    elif call_status != "completed":
        follow_up = "human_review"
    elif state.appointment_action and state.appointment_status != "completed":
        follow_up = "appointment_followup"
    else:
        follow_up = None
    if state.safety_decision == "transfer" and call_status == "completed":
        resolution = "transferred"
    elif follow_up is not None:
        resolution = "needs_follow_up"
    elif call_status == "completed":
        resolution = "completed"
    else:
        resolution = "unavailable"
    digest = sha256("\n".join(f"{turn.speaker}:{turn.text}" for turn in turns).encode()).hexdigest()
    return PostCallAnalysis(
        summary=summary,
        outcome=StructuredOutcome(
            call_status=call_status,
            resolution=resolution,
            intent=state.intent,
            language=state.language,
            safety_decision=state.safety_decision,
            appointment_action=state.appointment_action,
            follow_up=follow_up,
        ),
        transcript_digest=digest,
        turn_count=len(turns),
    )
