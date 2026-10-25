import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.conversation.locales import LanguagePolicy, base_language, normalize_language
from app.conversation.redaction import redact_transcript, redact_verification_secrets

# What one call keeps word for word; the saved transcript is the last 80 turns of it.
MAX_TURNS = 200
MAX_TRANSCRIPT_BYTES = 64_000


class SupportedLanguage(StrEnum):
    EN = "en"
    ES = "es"
    FR = "fr"
    DE = "de"
    PT = "pt"
    HI = "hi"
    HINGLISH = "hinglish"


class LanguageState(BaseModel):
    """Tracks conversation-level language and code-switching dynamics.

    Adheres to the architectural requirement that a single foreign word does not
    destabilize the conversation language, while code-switching between
    the shop's enabled languages is intentionally supported.
    """

    model_config = ConfigDict(extra="forbid")

    policy: LanguagePolicy = Field(default_factory=LanguagePolicy)
    primary_language: str = "en"
    confidence: float = Field(default=1.0, ge=0, le=1)
    code_switching: bool = False
    recent_languages: list[str] = Field(default_factory=list)
    history_window_size: int = Field(default=5, ge=2, le=20)

    def record_utterance(
        self,
        detected_language: str,
        confidence: float,
        threshold: float = 0.6,
        code_switching: bool = False,
    ) -> None:
        """Records an utterance language observation and updates primary language smoothly."""
        normalized = self._normalize_language(detected_language)
        if base_language(normalized) not in self.policy.enabled_languages or confidence < threshold:
            return
        self.recent_languages.append(normalized)
        if len(self.recent_languages) > self.history_window_size:
            self.recent_languages.pop(0)

        self.code_switching = (
            code_switching
            or "hinglish" in self.recent_languages
            or len(set(self.recent_languages)) > 1
        )

        # Do not immediately switch session language for low-confidence or single-word switches.
        # Shift primary_language only if sustained (>= 2 consecutive turns) or high confidence.
        if confidence >= threshold:
            consecutive_matches = sum(
                1 for lang in self.recent_languages[-2:] if lang == normalized
            )
            if consecutive_matches >= 2 or normalized == self.primary_language:
                self.primary_language = normalized
                self.confidence = confidence

    @staticmethod
    def _normalize_language(lang: str) -> str:
        return normalize_language(lang)


class CallStatus(StrEnum):
    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    TRANSFERRED = "transferred"
    DISCONNECTED = "disconnected"


class CallState(BaseModel):
    """Telephony and connection lifecycle state for the active phone call."""

    model_config = ConfigDict(extra="forbid")

    call_id: UUID
    tenant_id: UUID
    telephony_call_id: str
    phone_number_id: UUID | None = None
    caller_phone: str | None = None
    destination_phone: str | None = None
    stream_sid: str | None = None
    status: CallStatus = CallStatus.IN_PROGRESS
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    COLLECTING_IDENTIFIER = "collecting_identifier"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"


class IdentifierType(StrEnum):
    ACCESS_CODE = "access_code"
    PHONE = "phone"
    DOB = "dob"
    ACCOUNT_ID = "account_id"
    OTP = "otp"


class IdentityState(BaseModel):
    """Tracks caller identity verification state before exposing sensitive records."""

    model_config = ConfigDict(extra="forbid")

    status: VerificationStatus = VerificationStatus.UNVERIFIED
    patient_id: UUID | None = None
    caller_phone: str | None = None
    identifier_type: IdentifierType | None = None
    identifier_value: str | None = None
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    verified_at: datetime | None = None

    @property
    def is_verified(self) -> bool:
        return (
            self.status == VerificationStatus.VERIFIED
            and self.patient_id is not None
            and self.verified_at is not None
        )

    def start_collecting(self, identifier_type: IdentifierType) -> None:
        if self.attempts >= self.max_attempts:
            raise ValueError("Identity attempt limit reached")
        self.patient_id = None
        self.verified_at = None
        self.identifier_value = None
        self.status = VerificationStatus.COLLECTING_IDENTIFIER
        self.identifier_type = identifier_type

    def record_attempt(self, success: bool, patient_id: UUID | None = None) -> bool:
        if self.attempts >= self.max_attempts or self.status == VerificationStatus.FAILED:
            raise ValueError("Identity attempt limit reached")
        if success and patient_id is None:
            raise ValueError("Verified identity requires a patient identifier")
        self.attempts += 1
        self.patient_id = None
        self.verified_at = None
        self.identifier_value = None
        if success:
            self.status = VerificationStatus.VERIFIED
            self.patient_id = patient_id
            self.verified_at = datetime.now(UTC)
            return True
        if self.attempts >= self.max_attempts:
            self.status = VerificationStatus.FAILED
        else:
            self.status = VerificationStatus.COLLECTING_IDENTIFIER
        return False


class AppointmentDraft(BaseModel):
    """Draft state for collecting and selecting appointment parameters."""

    model_config = ConfigDict(extra="forbid")

    provider_id: UUID | None = None
    provider_name: str | None = None
    location_id: UUID | None = None
    location_name: str | None = None
    slot_id: UUID | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    service_type: str | None = None
    patient_notes: str | None = None


class FlowType(StrEnum):
    IDLE = "idle"
    APPOINTMENT_BOOKING = "appointment_booking"
    APPOINTMENT_RESCHEDULE = "appointment_reschedule"
    APPOINTMENT_CANCEL = "appointment_cancel"
    CLINIC_INQUIRY = "clinic_inquiry"
    EMERGENCY_ESCALATION = "emergency_escalation"
    HUMAN_TRANSFER = "human_transfer"


class FlowStep(StrEnum):
    INIT = "init"
    COLLECTING_DETAILS = "collecting_details"
    CHECKING_AVAILABILITY = "checking_availability"
    OFFERING_SLOTS = "offering_slots"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


def _empty_slot_list() -> list[dict[str, Any]]:
    return []


class ActiveFlowState(BaseModel):
    """Tracks active business workflow execution and collected slot values."""

    model_config = ConfigDict(extra="forbid")

    flow_type: FlowType = FlowType.IDLE
    step: FlowStep = FlowStep.INIT
    slots: dict[str, Any] = Field(default_factory=dict)
    offered_slots: list[dict[str, Any]] = Field(default_factory=_empty_slot_list)
    completed: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def set_slot(self, key: str, value: Any) -> None:
        self.slots[key] = value

    def get_slot(self, key: str, default: Any = None) -> Any:
        return self.slots.get(key, default)

    def transition(self, next_step: FlowStep) -> None:
        self.step = next_step
        self.completed = next_step == FlowStep.COMPLETED

    def reset(self) -> None:
        self.flow_type = FlowType.IDLE
        self.step = FlowStep.INIT
        self.slots.clear()
        self.offered_slots.clear()
        self.completed = False
        self.started_at = datetime.now(UTC)


class ConfirmationStatus(StrEnum):
    IDLE = "idle"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    EXPIRED = "expired"


class ConfirmationState(BaseModel):
    """Guards mutating domain actions with explicit caller confirmation."""

    model_config = ConfigDict(extra="forbid")

    status: ConfirmationStatus = ConfirmationStatus.IDLE
    action: str | None = None
    action_params: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = None
    requested_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def is_awaiting(self) -> bool:
        if self.status != ConfirmationStatus.AWAITING_CONFIRMATION:
            return False
        if self.expires_at is None or datetime.now(UTC) >= self.expires_at:
            self.status = ConfirmationStatus.EXPIRED
            return False
        return True

    def request(
        self,
        action: str,
        params: dict[str, Any],
        prompt: str,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.status = ConfirmationStatus.AWAITING_CONFIRMATION
        self.action = action
        self.action_params = deepcopy(params)
        self.prompt = prompt
        now = datetime.now(UTC)
        self.requested_at = now
        self.expires_at = now + timedelta(seconds=timeout_seconds)

    def confirm(self) -> dict[str, Any]:
        if not self.is_awaiting:
            raise ValueError("Cannot confirm: action is not awaiting confirmation or has expired")
        self.status = ConfirmationStatus.CONFIRMED
        return deepcopy(self.action_params)

    def deny(self) -> None:
        self.status = ConfirmationStatus.DENIED

    def reset(self) -> None:
        self.status = ConfirmationStatus.IDLE
        self.action = None
        self.action_params.clear()
        self.prompt = None
        self.requested_at = None
        self.expires_at = None


class SafetyDecision(StrEnum):
    CONTINUE = "continue"
    CLARIFY = "clarify"
    ESCALATE = "escalate"
    TRANSFER = "transfer"
    EMERGENCY_PROTOCOL = "emergency_protocol"
    BLOCK_TOOL = "block_tool"


class SafetyState(BaseModel):
    """Maintains independent safety status capable of overriding agent actions."""

    model_config = ConfigDict(extra="forbid")

    decision: SafetyDecision = SafetyDecision.CONTINUE
    is_emergency: bool = False
    reason: str | None = None
    source: str | None = None
    triggered_at: datetime | None = None
    context_hint: str = "none"
    context_turn: int = 0
    transfer_attempted: bool = False
    transfer_confirmed: bool = False
    event_recorded: bool = False
    # A transfer (or a message for the team) was offered; the caller has not answered yet.
    transfer_offered: bool = False
    offered_turn: int = 0
    transfer_target: str | None = Field(default=None, max_length=60)
    # How often the caller asked for a person: the second time, they are put through.
    human_asked: int = Field(default=0, ge=0)
    # What the caller needs, for the ticket and for whoever picks up.
    handoff_note: str | None = Field(default=None, max_length=500)
    # Nobody picked up the transfer and the call came back to the receptionist.
    transfer_returned: bool = False


class AppointmentOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["book_appointment", "cancel_appointment", "reschedule_appointment"]
    params: dict[str, Any]
    key: str
    summary: str
    status: Literal["proposed", "executing", "completed"] = "proposed"


class TranscriptTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speaker: Literal["caller", "agent"]
    text: str = Field(min_length=1, max_length=1000)
    language: str = Field(min_length=2, max_length=24)
    # What the model is shown: the caller's own email or phone number left in, so it can
    # use them during the call. Only verification secrets are hidden. Never stored: the
    # saved transcript has only `text`.
    model_text: str | None = Field(default=None, max_length=1000, exclude=True)


def _empty_transcript_turns() -> list[TranscriptTurn]:
    return []


class ConversationState(BaseModel):
    """The master conversation state object holding contextual call memory.

    Maintains context across multiple utterances without depending exclusively on
    raw LLM transcript history.
    """

    model_config = ConfigDict(extra="forbid")

    call_id: UUID
    tenant_id: UUID

    call_state: CallState
    language_state: LanguageState = Field(default_factory=LanguageState)
    identity_state: IdentityState = Field(default_factory=IdentityState)
    active_flow_state: ActiveFlowState = Field(default_factory=ActiveFlowState)
    confirmation_state: ConfirmationState = Field(default_factory=ConfirmationState)
    confirmation_timeout_seconds: float = Field(default=120, gt=0, le=600)
    safety_state: SafetyState = Field(default_factory=SafetyState)

    appointment_draft: AppointmentDraft | None = None
    appointment_operation: AppointmentOperation | None = None

    active_intent: str | None = None
    last_tool_name: str | None = None

    utterance_count: int = 0
    last_caller_utterance: str | None = None
    last_agent_response: str | None = None
    # The receptionist said goodbye and the call is to end once that has played.
    end_requested: bool = False
    # The caller talked over the last reply; what they did not hear of it.
    reply_cut_off: bool = False
    unsaid: str | None = Field(default=None, max_length=1000)
    transcript_turns: list[TranscriptTurn] = Field(
        default_factory=_empty_transcript_turns, max_length=MAX_TURNS
    )
    # Every turn ever added, so turns already summarized can be told apart.
    turns_appended: int = 0
    # A short summary of the part of a long call the model no longer sees word for word.
    earlier_summary: str | None = Field(default=None, max_length=2000)
    summarized_through: int = 0
    # The last few lookups, briefly, so a later turn can still refer to them.
    recent_lookups: list[str] = Field(default_factory=list[str], max_length=3)
    # Earlier calls and coming appointments of a trusted caller; kept for this call only.
    caller_history: str | None = Field(default=None, max_length=4000, exclude=True)
    # The first name to call them by: on file, or as they gave it. "" when none.
    caller_name: str | None = Field(default=None, max_length=120, exclude=True)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_call_binding(self) -> Self:
        if self.call_id != self.call_state.call_id or self.tenant_id != self.call_state.tenant_id:
            raise ValueError("Conversation call binding does not match")
        return self

    @property
    def awaiting_confirmation(self) -> bool:
        return self.confirmation_state.is_awaiting

    @property
    def language(self) -> str:
        return self.language_state.primary_language

    @property
    def language_confidence(self) -> float:
        return self.language_state.confidence

    @property
    def caller_id(self) -> UUID | None:
        return self.identity_state.patient_id

    def append_transcript_turn(self, speaker: Literal["caller", "agent"], text: str) -> None:
        clean = redact_transcript(text.strip())[:1000].strip()
        if not clean:
            return
        for_model = redact_verification_secrets(text.strip())[:1000].strip()
        self.transcript_turns.append(
            TranscriptTurn(
                speaker=speaker,
                text=clean,
                language=self.language[:24],
                model_text=for_model if for_model != clean else None,
            )
        )
        self.turns_appended += 1
        while len(self.transcript_turns) > MAX_TURNS or (
            sum(len(turn.text.encode("utf-8")) + 64 for turn in self.transcript_turns)
            > MAX_TRANSCRIPT_BYTES
        ):
            self.transcript_turns.pop(0)

    def record_lookup(self, tool: str, result: dict[str, Any]) -> None:
        digest = json.dumps(result, ensure_ascii=False, default=str)[:300]
        self.recent_lookups = [*self.recent_lookups, f"{tool}: {digest}"][-3:]

    @property
    def active_flow(self) -> str | None:
        return (
            self.active_flow_state.flow_type.value
            if self.active_flow_state.flow_type != FlowType.IDLE
            else None
        )
