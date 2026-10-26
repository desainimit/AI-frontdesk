"""Safety policy runs before appointment confirmation or LLM tool execution."""

import asyncio
import logging
import re
from datetime import UTC, datetime

from app.ai.wording import spoken
from app.conversation.state import ConversationState, FlowType, SafetyDecision
from app.safety import handover
from app.safety.classifier import SafetyClassifier
from app.safety.handover_messages import handover_message
from app.safety.messages import safety_message
from app.safety.numbers import EmergencyLines
from app.safety.rules import deterministic_rule

logger = logging.getLogger(__name__)


class SafetyEngine:
    def __init__(
        self,
        classifier: SafetyClassifier | None = None,
        *,
        transfer_available: bool = False,
        classifier_timeout_seconds: float = 2.0,
        ask_first: bool = False,
    ) -> None:
        self.classifier = classifier
        self.transfer_available = transfer_available
        self.classifier_timeout_seconds = classifier_timeout_seconds
        # On a call, a request for a person is answered with an offer, and the caller is
        # put through once they agree. By text, the team takes over at once.
        self.ask_first = ask_first
        # The numbers to give in an emergency; set from the clinic's country each turn.
        self.lines = EmergencyLines()

    def message(self, state: ConversationState) -> str:
        """The fixed sentence for the decision already in force."""
        return self._message(state, state.safety_state.decision)

    def _message(self, state: ConversationState, decision: SafetyDecision) -> str:
        return safety_message(
            state.language,
            decision,
            transfer_available=self.transfer_available,
            lines=self.lines,
            reason=state.safety_state.reason,
        )

    async def evaluate(
        self, state: ConversationState, utterance: str, *, semantic: bool = True
    ) -> str | None:
        """The fixed reply when safety takes over this turn, otherwise nothing.

        With `semantic=False` only the instant rules run; `semantic_check` then asks the
        model about the same words without holding the reply up.
        """
        safety = state.safety_state
        if safety.decision == SafetyDecision.BLOCK_TOOL:
            return spoken(state.language, "unavailable")
        if safety.decision in {
            SafetyDecision.EMERGENCY_PROTOCOL,
            SafetyDecision.TRANSFER,
            SafetyDecision.ESCALATE,
        }:
            return self._message(state, safety.decision)
        context_hint = (
            safety.context_hint if state.utterance_count - safety.context_turn <= 2 else "none"
        )
        rule = deterministic_rule(utterance, context_hint)
        decision, reason, source = rule.decision, rule.reason, "rule"
        # The runtime has already replaced the code with a fixed marker before
        # safety classification; an access-code-only turn needs no semantic call.
        access_code = re.fullmatch(r"\s*(?:\d{8}|\[call access code\])\s*", utterance) is not None
        if (
            semantic
            and self.classifier is not None
            and decision != SafetyDecision.EMERGENCY_PROTOCOL
            and not access_code
        ):
            try:
                async with asyncio.timeout(self.classifier_timeout_seconds):
                    signal = await self.classifier.classify(utterance, context_hint)
                if signal.kind == "emergency" and signal.confidence >= 0.75:
                    decision, reason, source = (
                        SafetyDecision.EMERGENCY_PROTOCOL,
                        signal.reason_code,
                        "semantic",
                    )
                elif (
                    signal.kind == "emergency"
                    and signal.confidence >= 0.5
                    and decision == SafetyDecision.CONTINUE
                ):
                    decision, reason, source = (
                        SafetyDecision.CLARIFY,
                        "possible_emergency",
                        "semantic",
                    )
                elif (
                    signal.kind == "human"
                    and signal.confidence >= 0.85
                    and decision != SafetyDecision.TRANSFER
                    # On a call the model offers a person itself when asked; the second
                    # opinion misread "can you repeat that?" as a request for one.
                    and not self.ask_first
                ):
                    decision, reason, source = (
                        SafetyDecision.TRANSFER,
                        "human_requested",
                        "semantic",
                    )
                elif (
                    signal.kind in {"urgent", "uncertain"}
                    and signal.confidence >= 0.65
                    and decision == SafetyDecision.CONTINUE
                ):
                    decision, reason, source = (
                        SafetyDecision.CLARIFY,
                        "possible_urgent_symptom",
                        "semantic",
                    )
            except Exception:
                # Rule alarms remain in force when a provider fails or times out.
                logger.warning(
                    "safety_classifier_unavailable", extra={"call_id": str(state.call_id)}
                )
        if rule.context_hint != "none":
            safety.context_hint = rule.context_hint
            safety.context_turn = state.utterance_count
        elif context_hint == "none":
            safety.context_hint = "none"
        if decision == SafetyDecision.TRANSFER and self.ask_first:
            return self._asked_for_person(state)
        if decision == SafetyDecision.TRANSFER and not self.transfer_available:
            decision = SafetyDecision.ESCALATE
        if decision == SafetyDecision.CONTINUE:
            safety.decision = decision
            safety.reason = None
            safety.triggered_at = None
            safety.source = None
            return None
        safety.decision = decision
        safety.is_emergency = decision == SafetyDecision.EMERGENCY_PROTOCOL
        safety.reason = reason
        safety.source = source
        safety.triggered_at = datetime.now(UTC)
        # A safety interrupt invalidates any pending appointment mutation.
        state.confirmation_state.reset()
        state.appointment_operation = None
        state.appointment_draft = None
        state.active_flow_state.reset()
        if decision == SafetyDecision.EMERGENCY_PROTOCOL:
            state.active_flow_state.flow_type = FlowType.EMERGENCY_ESCALATION
        elif decision in {SafetyDecision.TRANSFER, SafetyDecision.ESCALATE}:
            state.active_flow_state.flow_type = FlowType.HUMAN_TRANSFER
        return self._message(state, decision)

    def _asked_for_person(self, state: ConversationState) -> str:
        """Offer once; asked again, or already offered, put them through."""
        safety = state.safety_state
        safety.human_asked += 1
        if safety.human_asked > 1 or handover.pending(state):
            decision = handover.agree(state, can_transfer=self.transfer_available)
            safety.reason, safety.source = "human_requested", "caller"
            if decision == SafetyDecision.TRANSFER:
                state.active_flow_state.flow_type = FlowType.HUMAN_TRANSFER
                return handover_message(state.language, "connecting")
            return handover_message(state.language, "message_taken")
        handover.offer(state, None, state.last_caller_utterance)
        return handover_message(
            state.language, "offer_first" if self.transfer_available else "offer_message"
        )

    async def semantic_check(self, state: ConversationState, utterance: str) -> str | None:
        """The model's own reading of the same words, run beside the reply.

        It can only raise the alarm, never lower one the rules already raised.
        """
        if self.classifier is None or state.safety_state.decision != SafetyDecision.CONTINUE:
            return None
        return await self.evaluate(state, utterance)
