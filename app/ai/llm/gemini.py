"""Gemini adapter. Every request carries its own context; nothing is stored at Google."""

from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import httpx
from google.genai import Client, errors, types
from pydantic import BaseModel, SecretStr, ValidationError

from app.ai.llm.base import (
    AssistantTurn,
    ChatRequest,
    Completed,
    LLMBlockedError,
    LLMCredentialsError,
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

DEFAULT_MODEL = "gemini-3.8-flash"
ThinkingLevel = Literal["minimal", "low", "medium", "high"]

_BLOCKED = frozenset(
    {
        types.FinishReason.SAFETY,
        types.FinishReason.RECITATION,
        types.FinishReason.BLOCKLIST,
        types.FinishReason.PROHIBITED_CONTENT,
        types.FinishReason.SPII,
    }
)


def _failure(status: int) -> LLMProviderError:
    if status == 429:
        return LLMRateLimitError("LLM rate limit reached")
    if status in {408, 504}:
        return LLMTimeoutError("LLM request timed out")
    if status in {401, 403}:
        return LLMCredentialsError("LLM credentials were rejected")
    if status >= 500:
        return LLMUnavailableError("LLM is unavailable")
    return LLMRejectedError("LLM rejected the request")


def _contents(request: ChatRequest) -> list[types.Content]:
    contents: list[types.Content] = []
    for turn in request.turns:
        if isinstance(turn, UserTurn):
            contents.append(types.Content(role="user", parts=[types.Part(text=turn.text)]))
        elif isinstance(turn, ToolResult):
            part = types.Part(
                function_response=types.FunctionResponse(
                    id=turn.call_id, name=turn.name, response=turn.content
                )
            )
            previous = contents[-1] if contents else None
            # Results of calls made in one step go back together.
            if (
                previous is not None
                and previous.role == "user"
                and previous.parts
                and previous.parts[0].function_response is not None
            ):
                previous.parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))
        elif turn.provider_state:
            # The model's own parts, thought signatures included, exactly as it sent them.
            contents.append(
                types.Content(role="model", parts=cast(list[types.Part], turn.provider_state))
            )
        else:
            parts = [types.Part(text=turn.text)] if turn.text else []
            parts.extend(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.id, name=call.name, args=call.arguments or {}
                    )
                )
                for call in turn.tool_calls
            )
            contents.append(types.Content(role="model", parts=parts))
    return contents


class GeminiLLMProvider:
    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        *,
        thinking_level: ThinkingLevel | None = "low",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.get_secret_value():
            raise LLMCredentialsError("LLM credentials are not configured")
        # An explicit httpx client keeps one transport in every environment and lets
        # the adapter own its timeout. The SDK does not retry unless told to.
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._client = Client(
            api_key=api_key.get_secret_value(),
            http_options=types.HttpOptions(
                httpx_async_client=self._http, timeout=int(timeout_seconds * 1000)
            ),
        )
        self.model = model or DEFAULT_MODEL
        self._max_output_tokens = max_output_tokens
        self._thinking = (
            types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level.upper()))
            if thinking_level
            else None
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            max_output_tokens=self._max_output_tokens,
            thinking_config=self._thinking,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=tool.name,
                            description=tool.description,
                            parameters_json_schema=tool.parameters,
                        )
                        for tool in request.tools
                    ]
                )
            ]
            if request.tools
            else None,
            # The server validates and runs every tool itself.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        parts: list[types.Part] = []
        text: list[str] = []
        calls: list[ToolCall] = []
        finish: types.FinishReason | None = None
        usage = Usage()
        try:
            chunks = await self._client.aio.models.generate_content_stream(
                model=self.model, contents=cast(Any, _contents(request)), config=config
            )
            async for chunk in chunks:
                counts = chunk.usage_metadata
                if counts is not None:
                    usage = Usage(
                        counts.prompt_token_count or 0,
                        (counts.candidates_token_count or 0) + (counts.thoughts_token_count or 0),
                    )
                candidate = chunk.candidates[0] if chunk.candidates else None
                if candidate is None:
                    continue
                for part in (candidate.content.parts or []) if candidate.content else []:
                    parts.append(part)
                    if part.function_call is not None:
                        call = part.function_call
                        calls.append(
                            ToolCall(
                                call.id or f"call_{len(calls)}", call.name or "", call.args or {}
                            )
                        )
                    elif part.text and not part.thought:
                        text.append(part.text)
                        yield TextDelta(part.text)
                finish = candidate.finish_reason or finish
        except errors.APIError as error:
            raise _failure(error.code) from None
        except (httpx.TimeoutException, TimeoutError):
            raise LLMTimeoutError("LLM request timed out") from None
        except (httpx.HTTPError, OSError):
            raise LLMUnavailableError("LLM is unavailable") from None
        except ValueError:
            # The stream carried something that is not a response at all.
            raise LLMInvalidResponseError("LLM returned no usable response") from None
        if finish in _BLOCKED or (finish is None and not parts):
            raise LLMBlockedError("LLM returned no usable response")
        if finish not in {types.FinishReason.STOP, types.FinishReason.MAX_TOKENS} or (
            not calls and not "".join(text).strip()
        ):
            raise LLMInvalidResponseError("LLM returned no usable response")
        yield Completed(
            AssistantTurn("".join(text), tuple(calls), parts),
            "tool_calls"
            if calls
            else "length"
            if finish == types.FinishReason.MAX_TOKENS
            else "stop",
            usage,
            self.model,
        )

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        instructions = [m.content for m in request.messages if m.role != "user"]
        turns = [m.content for m in request.messages if m.role == "user"]
        config = types.GenerateContentConfig(
            system_instruction="\n\n".join(instructions) or None,
            max_output_tokens=self._max_output_tokens,
            thinking_config=self._thinking,
            response_mime_type="application/json",
            response_json_schema=wire_schema(schema.model_json_schema()),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=[
                    types.Content(role="user", parts=[types.Part(text=text)]) for text in turns
                ],
                config=config,
            )
        except errors.APIError as error:
            raise _failure(error.code) from None
        except (httpx.TimeoutException, TimeoutError):
            raise LLMTimeoutError("LLM request timed out") from None
        except (httpx.HTTPError, OSError):
            raise LLMUnavailableError("LLM is unavailable") from None
        candidate = response.candidates[0] if response.candidates else None
        if candidate is None or candidate.finish_reason in _BLOCKED:
            raise LLMBlockedError("LLM returned no usable response")
        if candidate.finish_reason != types.FinishReason.STOP:
            raise LLMInvalidResponseError("LLM returned no complete response")
        try:
            text = response.text
        except ValueError:
            text = None
        if not text:
            raise LLMInvalidResponseError("LLM returned no complete response")
        try:
            return schema.model_validate_json(text)
        except (ValidationError, ValueError):
            raise LLMInvalidResponseError("LLM returned no valid structured response") from None

    async def close(self) -> None:
        await self._client.aio.aclose()
        self._client.close()
        await self._http.aclose()
