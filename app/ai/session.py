"""Who the assistant is talking to, on which channel, for which clinic."""

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from app.auth.context import ActorType, RequestContext
from app.calls.service import CallContext
from app.conversation.state import CallStatus, ConversationState, SafetyDecision
from app.settings.schemas import ClinicConfiguration

Channel = Literal["voice", "sms", "test"]


class AssistantAuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Session:
    tenant_id: UUID
    # The call's own ID on the phone; the thread's ID for texts and the test chat.
    conversation_id: UUID
    channel: Channel
    request_id: UUID
    # The clinic's settings as the conversation started.
    configuration: ClinicConfiguration
    allowed_tools: frozenset[str]
    provider_call_id: str | None = None
    # The other person's phone number, when the channel tells us.
    contact: str | None = None
    # The clinic's doctors and services with their ids, read as the call started.
    directory: dict[str, Any] | None = None
    # Whose line the call came in on (a provider's or a location's), when not the clinic's.
    line: dict[str, str] | None = None

    @property
    def call(self) -> CallContext:
        if self.provider_call_id is None:
            raise AssistantAuthorizationError("Not a phone call")
        return CallContext(
            self.tenant_id, self.conversation_id, self.provider_call_id, self.request_id
        )

    @property
    def writes(self) -> bool:
        """A test chat looks things up but changes nothing."""
        return self.channel != "test"

    def validate_binding(self, state: ConversationState) -> None:
        if (
            state.tenant_id != self.tenant_id
            or state.call_id != self.conversation_id
            or state.call_state.status != CallStatus.IN_PROGRESS
            or state.call_state.ended_at is not None
        ):
            raise AssistantAuthorizationError("Session does not match the conversation")

    def validate(self, state: ConversationState) -> None:
        self.validate_binding(state)
        if (
            state.safety_state.decision != SafetyDecision.CONTINUE
            or state.safety_state.is_emergency
        ):
            raise AssistantAuthorizationError("Safety policy blocks assistant tools")

    def request(self, state: ConversationState) -> RequestContext:
        """The database identity for this turn: the patient, once the server knows them."""
        self.validate(state)
        patient = state.identity_state.patient_id if state.identity_state.is_verified else None
        if self.provider_call_id is not None:
            return RequestContext(
                self.request_id,
                self.tenant_id,
                None,
                ActorType.CALLER,
                call_id=self.conversation_id,
                provider_call_id=self.provider_call_id,
                patient_id=patient,
            )
        return RequestContext(
            self.request_id,
            self.tenant_id,
            None,
            ActorType.CALLER,
            conversation_id=self.conversation_id,
            patient_id=patient,
        )
