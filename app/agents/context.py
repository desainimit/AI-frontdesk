from dataclasses import dataclass

from app.agents.catalog import ALL_TOOLS as ALL_TOOLS
from app.agents.catalog import APPOINTMENT_TOOLS as APPOINTMENT_TOOLS
from app.agents.catalog import MUTATION_TOOLS as MUTATION_TOOLS
from app.agents.catalog import SAFE_READ_TOOLS as SAFE_READ_TOOLS
from app.calls.service import CallContext
from app.configuration.schemas import OperationalConfiguration
from app.conversation.state import CallStatus, ConversationState, SafetyDecision


class AgentAuthorizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentContext:
    call: CallContext
    allowed_tools: frozenset[str] = SAFE_READ_TOOLS
    business_name: str = ""
    instructions: str = ""
    tone: str = "professional"
    configuration_version: int | None = None
    configuration: OperationalConfiguration | None = None

    def validate_binding(self, state: ConversationState) -> None:
        if (
            state.tenant_id != self.call.tenant_id
            or state.call_id != self.call.call_id
            or state.call_state.tenant_id != self.call.tenant_id
            or state.call_state.call_id != self.call.call_id
            or state.call_state.telephony_call_id != self.call.provider_call_id
            or state.call_state.status != CallStatus.IN_PROGRESS
            or state.call_state.ended_at is not None
        ):
            raise AgentAuthorizationError("Agent context is not authorized")

    def validate(self, state: ConversationState) -> None:
        self.validate_binding(state)
        if (
            state.safety_state.decision != SafetyDecision.CONTINUE
            or state.safety_state.is_emergency
        ):
            raise AgentAuthorizationError("Safety policy blocks agent tools")
