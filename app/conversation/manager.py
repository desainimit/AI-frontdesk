import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.conversation.confirmation import confirmation_answer
from app.conversation.language import observe_language
from app.conversation.locales import LanguagePolicy
from app.conversation.redaction import redact_verification_secrets
from app.conversation.state import (
    ActiveFlowState,
    AppointmentDraft,
    CallState,
    ConfirmationState,
    ConversationState,
    FlowStep,
    FlowType,
    IdentityState,
    LanguageState,
    SafetyState,
)


class ConversationStateManager:
    """Orchestrates deterministic context maintenance across multiple speech turns.

    Maintains workflow, slot, language, and confirmation state so that short
    caller utterances like 'Tomorrow', 'Dr Shah', 'Afternoon', or 'Yes' resolve
    accurately without requiring the LLM to reconstruct context from raw transcripts.
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
        """Processes an incoming caller utterance, updating language, slots,

        confirmation, and intent.
        """
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

        # 3. Intent & Flow Recognition (if in IDLE state)
        if state.active_flow_state.flow_type == FlowType.IDLE:
            cls._detect_and_start_flow(state, clean_text)

        # 4. Contextual Slot Filling for Active Flow
        if state.active_flow_state.flow_type != FlowType.IDLE:
            cls._fill_slots_for_active_flow(state, clean_text)

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
    def _detect_and_start_flow(cls, state: ConversationState, text: str) -> None:
        lower = text.lower()
        booking_keywords = [
            "book",
            "appointment",
            "schedule",
            "mulaqat",
            "doctor se milna",
            "अपॉइंटमेंट",
            "डॉक्टर",
            "बुक",
            "see a doctor",
        ]
        if any(kw in lower for kw in booking_keywords):
            if "cancel" in lower or "radd" in lower:
                state.active_flow_state.flow_type = FlowType.APPOINTMENT_CANCEL
                state.active_flow_state.step = FlowStep.COLLECTING_DETAILS
                state.active_intent = "cancel_appointment"
            elif "reschedule" in lower or "badal" in lower or "postpone" in lower:
                state.active_flow_state.flow_type = FlowType.APPOINTMENT_RESCHEDULE
                state.active_flow_state.step = FlowStep.COLLECTING_DETAILS
                state.active_intent = "reschedule_appointment"
            else:
                state.active_flow_state.flow_type = FlowType.APPOINTMENT_BOOKING
                state.active_flow_state.step = FlowStep.COLLECTING_DETAILS
                state.active_intent = "book_appointment"
                if state.appointment_draft is None:
                    state.appointment_draft = AppointmentDraft()

    @classmethod
    def _fill_slots_for_active_flow(cls, state: ConversationState, text: str) -> None:
        lower = text.lower()
        flow = state.active_flow_state

        if flow.flow_type == FlowType.APPOINTMENT_BOOKING:
            if state.appointment_draft is None:
                state.appointment_draft = AppointmentDraft()

            # Provider extraction (e.g., "Dr. Shah", "Dr Mehta", "Doctor Sharma")
            provider_match = re.search(r"\b(?:dr\.?|doctor)\s+([a-zA-Z]+)", lower)
            if provider_match:
                name = f"Dr {provider_match.group(1).title()}"
                flow.set_slot("provider_name", name)
                state.appointment_draft.provider_name = name

            # Date extraction
            date_terms = [
                "today",
                "tomorrow",
                "kal",
                "aaj",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
            ]
            for date_term in date_terms:
                if date_term in lower:
                    flow.set_slot("date", date_term)
                    break

            # Time preference extraction
            time_terms = ["morning", "afternoon", "evening", "subah", "dopahar", "shaam"]
            for time_term in time_terms:
                if time_term in lower:
                    flow.set_slot("time_preference", time_term)
                    break

            # Specific time pattern (e.g., "4 pm", "10:30 am", "2:00")
            time_match = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", lower)
            if time_match and any(suffix in lower for suffix in ["am", "pm", "baje", "o'clock"]):
                flow.set_slot("exact_time", time_match.group(1).strip())

    @classmethod
    def propose_appointment_slot(
        cls,
        state: ConversationState,
        action_params: dict[str, Any],
        prompt: str,
        timeout_seconds: float | None = None,
    ) -> None:
        """Puts the active flow into AWAITING_CONFIRMATION and sets the confirmation prompt."""
        state.active_flow_state.transition(FlowStep.AWAITING_CONFIRMATION)
        state.confirmation_state.request(
            action="book_appointment",
            params=action_params,
            prompt=prompt,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else state.confirmation_timeout_seconds
            ),
        )

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
