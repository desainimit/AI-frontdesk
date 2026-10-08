"""Gemini adapter. Every request carries its own context; nothing is stored at Google."""

from typing import Literal

import httpx
from google.genai import Client, errors, types
from pydantic import BaseModel, SecretStr, ValidationError

from app.agents.providers.base import (
    LLMBlockedError,
    LLMCredentialsError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRejectedError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.agents.providers.schema import wire_schema

DEFAULT_MODEL = "gemini-3.8-flash"
ThinkingLevel = Literal["low", "medium", "high"]

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
        self._model = model or DEFAULT_MODEL
        self._max_output_tokens = max_output_tokens
        self._thinking = (
            types.ThinkingConfig(thinking_level=types.ThinkingLevel(thinking_level.upper()))
            if thinking_level
            else None
        )

    async def _generate(self, request: LLMRequest, schema: type[BaseModel] | None) -> str:
        instructions = [m.content for m in request.messages if m.role != "user"]
        turns = [m.content for m in request.messages if m.role == "user"]
        config = types.GenerateContentConfig(
            system_instruction="\n\n".join(instructions) or None,
            max_output_tokens=self._max_output_tokens,
            thinking_config=self._thinking,
            response_mime_type="application/json" if schema else None,
            response_json_schema=wire_schema(schema.model_json_schema()) if schema else None,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
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
        return text

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(await self._generate(request, None))

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        text = await self._generate(request, schema)
        try:
            return schema.model_validate_json(text)
        except (ValidationError, ValueError):
            raise LLMInvalidResponseError("LLM returned no valid structured response") from None

    async def close(self) -> None:
        await self._client.aio.aclose()
        self._client.close()
        await self._http.aclose()
