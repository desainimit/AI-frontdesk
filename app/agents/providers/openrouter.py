"""OpenRouter adapter. One request per call; nothing is retried or stored here."""

from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.agents.providers.base import (
    LLMBlockedError,
    LLMCredentialsError,
    LLMCreditsError,
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

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-3.8-flash"
ReasoningEffort = Literal["minimal", "low", "medium", "high"]

# Applied to every request: never route to an endpoint that keeps or trains on the
# conversation, never to one that would ignore the output schema, and prefer the
# cheapest endpoint so a pricier service tier is not picked by default.
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
    ) -> None:
        if not api_key.get_secret_value():
            raise LLMCredentialsError("LLM credentials are not configured")
        self._key = api_key
        self._http = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._model = model or DEFAULT_MODEL
        self._max_output_tokens = max_output_tokens
        self._effort = reasoning_effort

    async def _generate(self, request: LLMRequest, schema: type[BaseModel] | None) -> str:
        instructions = [m.content for m in request.messages if m.role != "user"]
        messages: list[dict[str, str]] = []
        if instructions:
            messages.append({"role": "system", "content": "\n\n".join(instructions)})
        messages.extend(
            {"role": "user", "content": m.content} for m in request.messages if m.role == "user"
        )
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            # Reasoning is billed as output and counts against this limit.
            "max_tokens": self._max_output_tokens,
            "provider": ROUTING,
        }
        if self._effort:
            body["reasoning"] = {"effort": self._effort, "exclude": True}
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": wire_schema(schema.model_json_schema()),
                },
            }
        try:
            response = await self._http.post(
                CHAT_URL,
                json=body,
                headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
            )
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
        # A generation failure can arrive inside a 200 response.
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
        await self._http.aclose()
