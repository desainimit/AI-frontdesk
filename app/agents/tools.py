import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.agents.context import ALL_TOOLS, AgentAuthorizationError, AgentContext
from app.agents.schemas import ToolCall
from app.calls.directory import CallDirectoryService, DirectoryQuery
from app.conversation.state import ConversationState


class ToolValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReadTool:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[[AgentContext, BaseModel], Awaitable[BaseModel]]


@dataclass(frozen=True)
class StatefulTool:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[[AgentContext, ConversationState, BaseModel], Awaitable[BaseModel]]


class ToolRegistry:
    def __init__(self, tools: list[ReadTool | StatefulTool], timeout_seconds: float = 3) -> None:
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("Duplicate tool registration")
        if any(tool.name not in ALL_TOOLS for tool in tools):
            raise ValueError("Only registered frontdesk capabilities may be exposed")
        self._tools = {tool.name: tool for tool in tools}
        self.timeout_seconds = timeout_seconds

    def registered_tools(self) -> list[ReadTool | StatefulTool]:
        return list(self._tools.values())

    def descriptions(self, context: AgentContext) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "arguments_schema": tool.schema.model_json_schema(),
            }
            for tool in self._tools.values()
            if tool.name in context.allowed_tools
        ]

    def validate(
        self, context: AgentContext, state: ConversationState, call: ToolCall
    ) -> tuple[ReadTool | StatefulTool, BaseModel]:
        context.validate(state)
        tool = self._tools.get(call.name)
        if tool is None or call.name not in context.allowed_tools:
            raise AgentAuthorizationError("Tool is not permitted")
        try:
            # JSON parser rejects duplicate keys as well as schema extra fields/coercion.
            def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                result: dict[str, Any] = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate arguments")
                    result[key] = value
                return result

            arguments = json.loads(call.arguments_json, object_pairs_hook=unique_pairs)
            validated = tool.schema.model_validate_json(json.dumps(arguments), strict=True)
            return tool, validated
        except (ValueError, ValidationError, RecursionError):
            raise ToolValidationError("Tool arguments are invalid") from None

    async def execute(
        self, context: AgentContext, state: ConversationState, call: ToolCall
    ) -> dict[str, Any]:
        tool, payload = self.validate(context, state, call)
        async with asyncio.timeout(self.timeout_seconds):
            result = (
                await tool.handler(context, state, payload)
                if isinstance(tool, StatefulTool)
                else await tool.handler(context, payload)
            )
        if len(result.model_dump_json()) > 16384:
            raise ToolValidationError("Tool result exceeds limit")
        return result.model_dump(mode="json")


def directory_tools(service: CallDirectoryService, timeout_seconds: float = 3) -> ToolRegistry:
    async def locations(context: AgentContext, payload: BaseModel) -> BaseModel:
        return await service.read(
            context.call, "locations", DirectoryQuery.model_validate(payload.model_dump())
        )

    async def providers(context: AgentContext, payload: BaseModel) -> BaseModel:
        return await service.read(
            context.call, "providers", DirectoryQuery.model_validate(payload.model_dump())
        )

    async def services(context: AgentContext, payload: BaseModel) -> BaseModel:
        return await service.read(
            context.call, "services", DirectoryQuery.model_validate(payload.model_dump())
        )

    return ToolRegistry(
        [
            ReadTool(
                "list_services",
                "List the appointment types this clinic offers, with their length in minutes. "
                "Pass an id as service_id when checking availability or booking.",
                DirectoryQuery,
                services,
            ),
            ReadTool(
                "list_locations",
                "List this clinic's locations and timezones.",
                DirectoryQuery,
                locations,
            ),
            ReadTool(
                "list_providers",
                "List this clinic's doctor names and specialties.",
                DirectoryQuery,
                providers,
            ),
        ],
        timeout_seconds,
    )
