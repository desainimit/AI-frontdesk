"""Safety policy runs before appointment confirmation or LLM tool execution."""

import asyncio
import logging
import re
from datetime import UTC, datetime

from app.agents.language import spoken
from app.conversation.state import ConversationState, FlowType, SafetyDecision
from app.safety.classifier import SafetyClassifier
from app.safety.messages import safety_message
from app.safety.rules import deterministic_rule

logger = logging.getLogger(__name__)


class SafetyEngine:
    def __init__(
        self,
        classifier: SafetyClassifier | None = None,
        *,
        transfer_available: bool = False,
        classifier_timeout_seconds: float = 2.0,
    ) -> None:
        self.classifier = classifier
        self.transfer_available = transfer_available
        self.classifier_timeout_seconds = classifier_timeout_seconds

    async def evaluate(self, state: ConversationState, utterance: str) -> str | None:
        safety = state.safety_state
        if safety.decision == SafetyDecision.BLOCK_TOOL:
            return spoken(state.language, "unavailable")
        if safety.decision in {
            SafetyDecision.EMERGENCY_PROTOCOL,
            SafetyDecision.TRANSFER,
            SafetyDecision.ESCALATE,
        }:
            return safety_message(
                state.language, safety.decision, transfer_available=self.transfer_available
            )
        context_hint = (
            safety.context_hint if state.utterance_count - safety.context_turn <= 2 else "none"
        )
        rule = deterministic_rule(utterance, context_hint)
        decision, reason, source = rule.decision, rule.reason, "rule"
        # The runtime has already replaced the code with a fixed marker before
        # safety classification; an access-code-only turn needs no semantic call.
        access_code = (
            re.fullmatch(r"\s*(?:[0-9]{8}|\[call access code\])\s*", utterance) is not None
        )
        if (
            self.classifier is not None
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
        if decision == SafetyDecision.TRANSFER and not self.transfer_available:
            decision = SafetyDecision.ESCALATE
        if rule.context_hint != "none":
            safety.context_hint = rule.context_hint
            safety.context_turn = state.utterance_count
        elif context_hint == "none":
            safety.context_hint = "none"
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
        return safety_message(state.language, decision, transfer_available=self.transfer_available)
