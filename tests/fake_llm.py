"""Stand-ins for the model. Offline and deterministic; never presented as a real model."""

from collections.abc import AsyncIterator, Callable
from typing import Any

from pydantic import BaseModel

from app.ai.llm.base import (
    AssistantTurn,
    ChatRequest,
    Completed,
    LLMRequest,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolResult,
    Usage,
    UserTurn,
)

Step = str | list[ToolCall] | Exception | Callable[[ChatRequest], "Step"]


class ScriptedModel:
    """Plays prepared steps back, one per model request, and remembers what it was asked."""

    model = "scripted"

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.requests: list[ChatRequest] = []
        self.closed = False

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        step = self.steps.pop(0)
        while callable(step):
            step = step(request)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, list):
            yield Completed(AssistantTurn("", tuple(step)), "tool_calls", Usage(10, 2), self.model)
            return
        # Arrive in small pieces, as a real stream does.
        for index in range(0, len(step), 7):
            yield TextDelta(step[index : index + 7])
        yield Completed(AssistantTurn(step), "stop", Usage(10, 5), self.model)

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        raise RuntimeError("No structured answer was scripted")

    async def close(self) -> None:
        self.closed = True


class MockLLMProvider:
    """A receptionist by keyword: enough to drive a whole call without a model."""

    model = "mock"

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        last = request.turns[-1]
        if isinstance(last, ToolResult):
            rows: list[dict[str, Any]] = last.content.get("items", [])
            names = ", ".join(str(row.get("name", "")) for row in rows) or "nothing"
            text = f"I found: {names}."
        else:
            said = last.text.lower() if isinstance(last, UserTurn) else ""
            tool = (
                "list_providers"
                if any(word in said for word in ("doctor", "provider", "dr "))
                else "list_locations"
                if any(word in said for word in ("clinic", "location", "timezone"))
                else None
            )
            if tool is not None and any(spec.name == tool for spec in request.tools):
                yield Completed(
                    AssistantTurn("", (ToolCall("call_1", tool, {}),)),
                    "tool_calls",
                    Usage(),
                    "mock",
                )
                return
            text = "Hello. I can help with clinic and doctor information."
        yield TextDelta(text)
        yield Completed(AssistantTurn(text), "stop", Usage(), "mock")

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        raise RuntimeError("The mock gives no structured answers")

    async def close(self) -> None:
        return None
