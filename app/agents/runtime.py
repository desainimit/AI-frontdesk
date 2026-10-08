import asyncio
import logging
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from app.agents.appointment_flow import AppointmentFlow
from app.agents.context import MUTATION_TOOLS, AgentAuthorizationError, AgentContext
from app.agents.language import spoken
from app.agents.prompts import build_request
from app.agents.providers.base import LLMProvider
from app.agents.schemas import AgentDecision, AgentResult, Intent
from app.agents.tools import ToolRegistry, ToolValidationError
from app.conversation.manager import ConversationStateManager
from app.conversation.redaction import redact_verification_secrets
from app.conversation.state import ConversationState, SafetyDecision
from app.safety.engine import SafetyEngine
from app.voice.metrics import VoiceMetrics

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        *,
        appointment_flow: AppointmentFlow | None = None,
        safety_engine: SafetyEngine | None = None,
        max_steps: int = 3,
        max_tool_calls: int = 4,
        turn_timeout_seconds: float = 20,
        metrics: VoiceMetrics | None = None,
    ) -> None:
        self.appointment_flow = appointment_flow
        self.safety_engine = safety_engine or SafetyEngine()
        self.provider = provider
        self.registry = registry
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.turn_timeout_seconds = turn_timeout_seconds
        self.metrics = metrics
        self._lock = asyncio.Lock()

    async def respond(
        self, context: AgentContext, state: ConversationState, utterance: str
    ) -> AgentResult:
        tools_used: list[str] = []
        try:
            async with asyncio.timeout(self.turn_timeout_seconds), self._lock:
                context.validate_binding(state)
                if not utterance.strip() or len(utterance) > 4000:
                    raise ToolValidationError("Invalid utterance size")
                safe_utterance = redact_verification_secrets(utterance)
                safety_response = await self.safety_engine.evaluate(state, safe_utterance)
                if safety_response is not None:
                    decision = state.safety_state.decision
                    intent = (
                        Intent.EMERGENCY
                        if decision == SafetyDecision.EMERGENCY_PROTOCOL
                        else Intent.HUMAN_TRANSFER
                        if decision in {SafetyDecision.TRANSFER, SafetyDecision.ESCALATE}
                        else Intent.UNKNOWN
                    )
                    state.active_intent = intent.value
                    ConversationStateManager.record_agent_response(state, safety_response)
                    return AgentResult(intent=intent, response=safety_response, status="blocked")
                context.validate(state)
                if self.appointment_flow is not None:
                    direct = await self.appointment_flow.before_turn(context, state, utterance)
                    if direct is not None:
                        return direct
                results: list[dict[str, Any]] = []
                for _ in range(self.max_steps):
                    context.validate(state)
                    request = build_request(context, state, safe_utterance, self.registry, results)
                    started = perf_counter()
                    try:
                        raw = await self.provider.generate_structured(request, AgentDecision)
                    finally:
                        if self.metrics is not None:
                            self.metrics.record_llm_latency(
                                (perf_counter() - started) * 1000, type(self.provider).__name__
                            )
                    # Even mocks/custom providers cross an untrusted validation boundary.
                    decision = AgentDecision.model_validate_json(raw.model_dump_json())
                    context.validate(state)
                    if not decision.tool_calls:
                        state.active_intent = decision.intent.value
                        ConversationStateManager.record_agent_response(state, decision.response)
                        return AgentResult(
                            intent=decision.intent,
                            response=decision.response,
                            tools_used=tools_used,
                        )
                    if len(tools_used) + len(decision.tool_calls) > self.max_tool_calls:
                        raise ToolValidationError("Tool budget exhausted")
                    if (
                        any(call.name in MUTATION_TOOLS for call in decision.tool_calls)
                        and len(decision.tool_calls) != 1
                    ):
                        raise ToolValidationError("Propose one mutation at a time")
                    # Reject the whole batch before any tool is executed.
                    for call in decision.tool_calls:
                        self.registry.validate(context, state, call)
                    for call in decision.tool_calls:
                        result = await self.registry.execute(context, state, call)
                        tools_used.append(call.name)
                        ConversationStateManager.record_tool_call(state, call.name)
                        results.append({"name": call.name, "result": result})
                        if call.name == "get_knowledge" and result.get("status") == "not_found":
                            message = (
                                context.configuration.agent.unknown_answer
                                if context.configuration
                                and state.language
                                == context.configuration.languages.default_language
                                else spoken(state.language, "knowledge_unavailable")
                            )
                            ConversationStateManager.record_agent_response(state, message)
                            return AgentResult(
                                intent=Intent.CLINIC_INQUIRY,
                                response=message,
                                tools_used=tools_used,
                            )
                        if call.name in MUTATION_TOOLS:
                            message = str(result["message"])
                            ConversationStateManager.record_agent_response(state, message)
                            return AgentResult(
                                intent=Intent(call.name), response=message, tools_used=tools_used
                            )
                raise ToolValidationError("Agent step budget exhausted")
        except AgentAuthorizationError:
            return AgentResult(
                intent=Intent.UNKNOWN,
                response=spoken(state.language, "unavailable"),
                tools_used=tools_used,
                status="blocked",
            )
        except (ToolValidationError, ValidationError):
            logger.warning("agent_output_rejected", extra={"call_id": str(context.call.call_id)})
        except Exception:
            # Never log vendor exception bodies, prompts, transcripts or generated arguments.
            logger.warning("agent_turn_unavailable", extra={"call_id": str(context.call.call_id)})
        return AgentResult(
            intent=Intent.UNKNOWN,
            response=spoken(state.language, "unavailable"),
            tools_used=tools_used,
            status="unavailable",
        )
