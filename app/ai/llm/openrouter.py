"""OpenRouter adapter. One request per call; nothing is retried or stored here."""

import json
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.ai.llm.base import (
    AssistantTurn,
    ChatRequest,
    Completed,
    LLMBlockedError,
    LLMCredentialsError,
    LLMCreditsError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRejectedError,
    LLMRequest,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolResult,
    Usage,
    UserTurn,
)
from app.ai.llm.schema import wire_schema

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-3.8-flash"
ReasoningEffort = Literal["minimal", "low", "medium", "high"]

# Applied to every request: never route to an endpoint that keeps or trains on the
# conversation, never to one that would ignore the output schema or the tools, and
# prefer the cheapest endpoint so a pricier service tier is not picked by default.
ROUTING: dict[str, Any] = {
    "data_collection": "deny",
    "require_parameters": True,
    "sort": "price",
}


def _failure(status: int) -> LLMProviderError:
    if status == 429:
        return LLMRateLimitError("LLM rate limit reached")
    if status == 408:
        return LLMTimeoutError("LLM request timed out")
    if status == 401:
        return LLMCredentialsError("LLM credentials were rejected")
    if status == 402:
        return LLMCreditsError("LLM account has no remaining credit")
    if status >= 500:
        return LLMUnavailableError("LLM is unavailable")
    return LLMRejectedError("LLM rejected the request")


def _messages(request: ChatRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
    for turn in request.turns:
        if isinstance(turn, UserTurn):
            messages.append({"role": "user", "content": turn.text})
        elif isinstance(turn, ToolResult):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": turn.call_id,
                    "content": json.dumps(turn.content, ensure_ascii=False),
                }
            )
        else:
            message: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
            if turn.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments or {}, ensure_ascii=False),
                        },
                    }
                    for call in turn.tool_calls
                ]
            if turn.provider_state:
                # The model's own reasoning record, which it needs back to go on.
                message["reasoning_details"] = turn.provider_state
            messages.append(message)
    return messages


class OpenRouterLLMProvider:
    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        *,
        reasoning_effort: ReasoningEffort | None = "low",
        http_client: httpx.AsyncClient | None = None,
        fastest: bool = False,
    ) -> None:
        if not api_key.get_secret_value():
            raise LLMCredentialsError("LLM credentials are not configured")
        self._key = api_key
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self.model = model or DEFAULT_MODEL
        self._max_output_tokens = max_output_tokens
        self._effort = reasoning_effort
        # On a call the quickest endpoint matters more than the cheapest.
        self._routing = ROUTING | {"sort": "latency"} if fastest else ROUTING

    def _body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            # Reasoning is billed as output and counts against this limit.
            "max_tokens": self._max_output_tokens,
            "provider": self._routing,
        }
        if self._effort:
            body["reasoning"] = {"effort": self._effort}
        return body

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key.get_secret_value()}"}

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        body = self._body() | {
            "messages": _messages(request),
            "stream": True,
            "usage": {"include": True},
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        text: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        reasoning: list[Any] = []
        finish: str | None = None
        usage = Usage()
        try:
            async with self._http.stream(
                "POST", CHAT_URL, json=body, headers=self._headers()
            ) as response:
                if response.status_code != 200:
                    raise _failure(response.status_code)
                async for line in response.aiter_lines():
                    # Blank lines separate events; lines starting with a colon keep the
                    # connection open while the provider is still working.
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = cast(dict[str, Any], json.loads(data))
                    except ValueError:
                        raise LLMInvalidResponseError("LLM returned no usable response") from None
                    # A generation failure can arrive inside a 200 response.
                    if "error" in payload:
                        raise LLMUnavailableError("LLM is unavailable")
                    reported = payload.get("usage")
                    if isinstance(reported, dict):
                        counts = cast(dict[str, Any], reported)
                        usage = Usage(
                            int(counts.get("prompt_tokens") or 0),
                            int(counts.get("completion_tokens") or 0),
                        )
                    choices = cast(list[dict[str, Any]], payload.get("choices") or [])
                    if not choices:
                        continue
                    choice = choices[0]
                    if "error" in choice:
                        raise LLMUnavailableError("LLM is unavailable")
                    delta = cast(dict[str, Any], choice.get("delta") or {})
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        text.append(piece)
                        yield TextDelta(piece)
                    for part in cast(list[dict[str, Any]], delta.get("tool_calls") or []):
                        call = calls.setdefault(
                            int(part.get("index") or 0), {"id": "", "name": "", "arguments": ""}
                        )
                        function = cast(dict[str, Any], part.get("function") or {})
                        call["id"] = call["id"] or str(part.get("id") or "")
                        call["name"] = call["name"] or str(function.get("name") or "")
                        call["arguments"] += str(function.get("arguments") or "")
                    reasoning.extend(cast(list[Any], delta.get("reasoning_details") or []))
                    finish = choice.get("finish_reason") or finish
        except httpx.TimeoutException:
            raise LLMTimeoutError("LLM request timed out") from None
        except httpx.HTTPError:
            raise LLMUnavailableError("LLM is unavailable") from None
        if finish == "content_filter":
            raise LLMBlockedError("LLM returned no usable response")
        if finish == "error":
            raise LLMUnavailableError("LLM is unavailable")
        tool_calls = tuple(
            ToolCall(call["id"] or f"call_{index}", call["name"], _arguments(call["arguments"]))
            for index, call in sorted(calls.items())
        )
        if not tool_calls and not "".join(text).strip():
            raise LLMInvalidResponseError("LLM returned no usable response")
        yield Completed(
            AssistantTurn("".join(text), tool_calls, reasoning or None),
            "tool_calls" if tool_calls else "length" if finish == "length" else "stop",
            usage,
            self.model,
        )

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        instructions = [m.content for m in request.messages if m.role != "user"]
        messages: list[dict[str, str]] = []
        if instructions:
            messages.append({"role": "system", "content": "\n\n".join(instructions)})
        messages.extend(
            {"role": "user", "content": m.content} for m in request.messages if m.role == "user"
        )
        body = self._body() | {
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": wire_schema(schema.model_json_schema()),
                },
            },
        }
        if self._effort:
            body["reasoning"] = {"effort": self._effort, "exclude": True}
        try:
            response = await self._http.post(CHAT_URL, json=body, headers=self._headers())
        except httpx.TimeoutException:
            raise LLMTimeoutError("LLM request timed out") from None
        except httpx.HTTPError:
            raise LLMUnavailableError("LLM is unavailable") from None
        if response.status_code != 200:
            raise _failure(response.status_code)
        try:
            payload = cast(dict[str, Any], response.json())
            choices = cast(list[dict[str, Any]], payload.get("choices") or [])
        except ValueError:
            raise LLMInvalidResponseError("LLM returned no complete response") from None
        if "error" in payload or not choices or "error" in choices[0]:
            raise LLMUnavailableError("LLM is unavailable")
        choice = choices[0]
        finish = choice.get("finish_reason")
        if finish == "content_filter":
            raise LLMBlockedError("LLM returned no usable response")
        if finish == "error":
            raise LLMUnavailableError("LLM is unavailable")
        text = cast(dict[str, Any], choice.get("message") or {}).get("content")
        if finish != "stop" or not isinstance(text, str) or not text:
            raise LLMInvalidResponseError("LLM returned no complete response")
        try:
            return schema.model_validate_json(text)
        except (ValidationError, ValueError):
            raise LLMInvalidResponseError("LLM returned no valid structured response") from None

    async def close(self) -> None:
        await self._http.aclose()


def _arguments(raw: str) -> dict[str, Any] | None:
    try:
        parsed: object = json.loads(raw or "{}")
    except ValueError:
        return None
    return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
