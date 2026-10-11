import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.ai.catalog import ALL_TOOLS
from app.ai.llm.base import ToolCall, ToolSpec
from app.ai.llm.schema import wire_schema
from app.ai.session import AssistantAuthorizationError, Session
from app.conversation.state import ConversationState


class ToolValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[[Session, ConversationState, BaseModel], Awaitable[BaseModel]]


class ToolRegistry:
    def __init__(self, tools: list[Tool], timeout_seconds: float = 5) -> None:
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("Duplicate tool registration")
        if any(tool.name not in ALL_TOOLS for tool in tools):
            raise ValueError("Only registered frontdesk capabilities may be exposed")
        self._tools = {tool.name: tool for tool in tools}
        self.timeout_seconds = timeout_seconds

    def specs(self, session: Session) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(tool.name, tool.description, wire_schema(tool.schema.model_json_schema()))
            for tool in self._tools.values()
            if tool.name in session.allowed_tools
        )

    def validate(
        self, session: Session, state: ConversationState, call: ToolCall
    ) -> tuple[Tool, BaseModel]:
        session.validate(state)
        tool = self._tools.get(call.name)
        if tool is None or call.name not in session.allowed_tools:
            raise AssistantAuthorizationError("Tool is not permitted")
        if call.arguments is None:
            raise ToolValidationError("Tool arguments are invalid")
        try:
            # Strict: no extra fields and no coercion of what the model wrote.
            return tool, tool.schema.model_validate_json(json.dumps(call.arguments), strict=True)
        except (ValueError, ValidationError, RecursionError):
            raise ToolValidationError("Tool arguments are invalid") from None

    async def execute(
        self, session: Session, state: ConversationState, call: ToolCall
    ) -> dict[str, Any]:
        tool, payload = self.validate(session, state, call)
        async with asyncio.timeout(self.timeout_seconds):
            result = await tool.handler(session, state, payload)
        if len(result.model_dump_json()) > 16384:
            raise ToolValidationError("Tool result exceeds limit")
        return result.model_dump(mode="json")
