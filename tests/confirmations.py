"""Put a conversation into "waiting for a yes or no", as a proposal from a tool does."""

from typing import Any

from app.conversation.state import ConversationState, FlowStep


def ask_to_confirm(
    state: ConversationState,
    action_params: dict[str, Any],
    prompt: str,
    timeout_seconds: float | None = None,
) -> None:
    state.active_flow_state.transition(FlowStep.AWAITING_CONFIRMATION)
    state.confirmation_state.request(
        action="book_appointment",
        params=action_params,
        prompt=prompt,
        timeout_seconds=timeout_seconds
        if timeout_seconds is not None
        else state.confirmation_timeout_seconds,
    )
