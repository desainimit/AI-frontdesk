from datetime import UTC, datetime
from uuid import UUID

from app.conversation.confirmation import confirmation_answer
from app.conversation.language import observe_language
from app.conversation.locales import LanguagePolicy
from app.conversation.redaction import redact_verification_secrets
from app.conversation.state import (
    ActiveFlowState,
    CallState,
    ConfirmationState,
    ConversationState,
    FlowStep,
    IdentityState,
    LanguageState,
    SafetyState,
)


class ConversationStateManager:
    """What the server itself keeps track of between turns: the language, the
    transcript, and whether a plain yes or no answers a question it asked.

    What the person wants is the model's job; nothing here guesses it from keywords.
    """

    @classmethod
    def create_initial_state(
        cls,
        call_id: UUID,
        tenant_id: UUID,
        telephony_call_id: str,
        caller_phone: str | None = None,
        default_language: str = "en",
        confirmation_timeout_seconds: float = 120,
        language_policy: LanguagePolicy | None = None,
    ) -> ConversationState:
        """Initializes a new empty ConversationState for an incoming call."""
        call_state = CallState(
            call_id=call_id,
            tenant_id=tenant_id,
            telephony_call_id=telephony_call_id,
            caller_phone=caller_phone,
        )
        policy = language_policy or LanguagePolicy(default_language=default_language)
        language_state = LanguageState(primary_language=policy.default_language, policy=policy)
        identity_state = IdentityState(caller_phone=caller_phone)
        return ConversationState(
            call_id=call_id,
            tenant_id=tenant_id,
            call_state=call_state,
            language_state=language_state,
            identity_state=identity_state,
            active_flow_state=ActiveFlowState(),
            confirmation_state=ConfirmationState(),
            confirmation_timeout_seconds=confirmation_timeout_seconds,
            safety_state=SafetyState(),
        )

    @classmethod
    def process_caller_utterance(
        cls,
        state: ConversationState,
        utterance: str,
        *,
        detected_language: str | None = None,
        detected_languages: tuple[str, ...] = (),
        confidence: float | None = None,
        language_threshold: float = 0.6,
    ) -> None:
        """Take in what the person just said: language, transcript, and a pending yes/no."""
        clean_text = utterance.strip()
        if not clean_text:
            return

        now = datetime.now(UTC)
        state.utterance_count += 1
        state.last_caller_utterance = redact_verification_secrets(clean_text)
        state.updated_at = now

        # 1. Update Language & Code-Switching Detection
        cls._update_language(
            state, clean_text, detected_language, language_threshold, detected_languages
        )
        state.append_transcript_turn("caller", clean_text)

        # 2. Check for Pending Confirmation Resolution
        if state.confirmation_state.is_awaiting:
            resolved = cls._resolve_confirmation(state, clean_text)
            if resolved:
                return
            # A changed or ambiguous reply invalidates the old proposal. A later yes
            # must never approve stale parameters.
            state.confirmation_state.reset()
            state.active_flow_state.transition(FlowStep.COLLECTING_DETAILS)

    @classmethod
    def _update_language(
        cls,
        state: ConversationState,
        text: str,
        detected_language: str | None,
        threshold: float,
        detected_languages: tuple[str, ...] = (),
    ) -> None:
        if not state.language_state.policy.auto_detect:
            return
        observation = observe_language(
            text,
            detected_language,
            provider_languages=detected_languages,
            enabled_languages=tuple(state.language_state.policy.enabled_languages),
        )
        if observation is not None:
            # A substantive first utterance can establish the language immediately;
            # LanguageState still requires sustained evidence for later switches.
            if not state.language_state.recent_languages and observation.confidence >= threshold:
                state.language_state.primary_language = observation.language
            state.language_state.record_utterance(
                detected_language=observation.language,
                confidence=observation.confidence,
                threshold=threshold,
                code_switching=observation.code_switching,
            )

    @classmethod
    def _resolve_confirmation(cls, state: ConversationState, text: str) -> bool:
        answer = confirmation_answer(text, state.language)
        if answer is None:
            return False
        if answer:
            state.confirmation_state.confirm()
            next_step = FlowStep.EXECUTING
        else:
            state.confirmation_state.deny()
            next_step = FlowStep.OFFERING_SLOTS
        if state.active_flow_state.step == FlowStep.AWAITING_CONFIRMATION:
            state.active_flow_state.transition(next_step)
        return True

    @classmethod
    def record_agent_response(cls, state: ConversationState, response_text: str) -> None:
        """Records the assistant's speech response into conversation state."""
        state.last_agent_response = response_text.strip()
        state.updated_at = datetime.now(UTC)

    @classmethod
    def record_tool_call(cls, state: ConversationState, tool_name: str) -> None:
        """Records the most recently executed tool name."""
        state.last_tool_name = tool_name
        state.updated_at = datetime.now(UTC)
