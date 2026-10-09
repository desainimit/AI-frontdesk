import json

from pydantic import BaseModel

from app.agents.providers.base import LLMRequest, LLMResponse
from app.agents.schemas import AgentDecision, Intent, ToolCall


class MockLLMProvider:
    """Deterministic offline demonstration; never presented as a real model."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        decision = await self.generate_structured(request, AgentDecision)
        return LLMResponse(decision.response)

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        payload = json.loads(request.messages[-1].content)
        text = str(payload["utterance"]).lower()
        tool_results = payload["tool_results"]
        previous = payload["conversation"].get("active_intent")
        intent = Intent.UNKNOWN
        tool = None
        response = "Please tell me whether you need clinic or doctor information."
        if "cancel" in text:
            intent = Intent.CANCEL_APPOINTMENT
        elif "reschedule" in text or "postpone" in text:
            intent = Intent.RESCHEDULE_APPOINTMENT
        elif any(word in text for word in ("book", "appointment", "schedule", "chahiye")):
            intent = Intent.BOOK_APPOINTMENT
        elif any(word in text for word in ("doctor", "provider", "dr ")):
            intent, tool = Intent.PROVIDER_INQUIRY, "list_providers"
        elif any(word in text for word in ("clinic", "location", "timezone")):
            intent, tool = Intent.CLINIC_INQUIRY, "list_locations"
        elif any(word in text for word in ("human", "receptionist")):
            intent = Intent.HUMAN_TRANSFER
        elif text in {"hi", "hello", "hey"}:
            intent, response = (
                Intent.GREETING,
                "Hello. I can help with clinic and doctor information.",
            )
        elif previous in {item.value for item in Intent}:
            intent = Intent(previous)
        if intent in {
            Intent.BOOK_APPOINTMENT,
            Intent.CANCEL_APPOINTMENT,
            Intent.RESCHEDULE_APPOINTMENT,
            Intent.HUMAN_TRANSFER,
        }:
            response = (
                "Please contact reception for that request. "
                "I can currently read directory information."
            )
        calls: list[ToolCall] = []
        if tool and not tool_results:
            calls = [ToolCall(name=tool, arguments_json='{"limit": 5, "offset": 0}')]
            response = ""
        elif tool_results:
            rows = tool_results[-1]["result"]["items"]
            names = [str(row.get("name", row.get("display_name", ""))) for row in rows]
            response = (
                "Available directory entries: " + ", ".join(names)
                if names
                else ("There are no matching directory entries. Please contact reception.")
            )
        decision = AgentDecision(intent=intent, response=response[:1000], tool_calls=calls)
        return schema.model_validate_json(decision.model_dump_json())

    async def close(self) -> None:
        pass
