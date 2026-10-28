import json
from enum import StrEnum
from typing import Any, Self

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from app.ai.llm.base import (
    LLMBlockedError,
    LLMCredentialsError,
    LLMCreditsError,
    LLMInvalidResponseError,
    LLMMessage,
    LLMProviderError,
    LLMRateLimitError,
    LLMRejectedError,
    LLMRequest,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.ai.llm.gemini import GeminiLLMProvider
from app.ai.llm.openrouter import OpenRouterLLMProvider
from app.ai.llm.schema import wire_schema
from app.safety.classifier import SafetySignal


class Intent(StrEnum):
    GREETING = "greeting"
    UNKNOWN = "unknown"


class Step(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    arguments_json: str = Field(min_length=2, max_length=4096)


class AgentDecision(BaseModel):
    """A structured answer with a nested model, an enum and a rule of its own."""

    model_config = ConfigDict(extra="forbid")
    intent: Intent
    response: str = Field(max_length=1000)
    tool_calls: list[Step] = Field(max_length=3)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.tool_calls and not self.response.strip():
            raise ValueError("A final decision requires a response")
        return self


REQUEST = LLMRequest(
    (
        LLMMessage("system", "Policy"),
        LLMMessage("developer", "Tools"),
        LLMMessage("user", "Hello"),
    )
)
FAILURES: dict[str, type[LLMProviderError]] = {
    "blocked": LLMBlockedError,
    "incomplete": LLMInvalidResponseError,
    "invalid": LLMInvalidResponseError,
    "empty": LLMBlockedError,
    "rate_limit": LLMRateLimitError,
    "bad_key": LLMCredentialsError,
    "unknown_model": LLMRejectedError,
    "outage": LLMUnavailableError,
    "timeout": LLMTimeoutError,
    "offline": LLMUnavailableError,
}
STATUS = {"rate_limit": 429, "bad_key": 403, "unknown_model": 404, "outage": 503}


def reply(outcome: str) -> httpx.Response:
    if outcome in STATUS:
        return httpx.Response(
            STATUS[outcome],
            json={"error": {"code": STATUS[outcome], "message": "private-provider-body"}},
        )
    if outcome == "empty":
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    text = json.dumps({"intent": "greeting", "response": "Hello.", "tool_calls": []})
    if outcome == "invalid":
        text = '{"intent":"unknown","response":"","tool_calls":[]}'
    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": [{"text": text}]},
        "finishReason": {"incomplete": "MAX_TOKENS", "blocked": "SAFETY"}.get(outcome, "STOP"),
    }
    if outcome == "blocked":
        del candidate["content"]
    return httpx.Response(200, json={"candidates": [candidate]})


@pytest.mark.parametrize("outcome", ["valid", *FAILURES])
async def test_gemini_adapter_contract(outcome: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("private-timeout", request=request)
        if outcome == "offline":
            raise httpx.ConnectError("private-connect", request=request)
        return reply(outcome)

    provider = GeminiLLMProvider(
        SecretStr("test-key"),
        "test-model",
        1,
        800,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        if outcome == "valid":
            result = await provider.generate_structured(REQUEST, AgentDecision)
            assert result.response == "Hello."
        else:
            with pytest.raises(LLMProviderError) as error:
                await provider.generate_structured(REQUEST, AgentDecision)
            assert type(error.value) is FAILURES[outcome]
            assert "private" not in str(error.value)
        # A failed turn is reported once; retry policy belongs to the caller.
        assert len(seen) == 1
        request = seen[0]
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.path.endswith("/models/test-model:generateContent")
        assert request.headers["x-goog-api-key"] == "test-key"
        assert "test-key" not in str(request.url)
        body = json.loads(request.content)
        assert body["systemInstruction"]["parts"][0]["text"] == "Policy\n\nTools"
        assert [part["text"] for item in body["contents"] for part in item["parts"]] == ["Hello"]
        generation = body["generationConfig"]
        assert generation["maxOutputTokens"] == 800
        assert generation["responseMimeType"] == "application/json"
        # The SDK forwards this object as given; the API accepts either spelling.
        thinking = generation["thinkingConfig"]
        assert (thinking.get("thinkingLevel") or thinking.get("thinking_level")) == "LOW"
        # Sampling parameters are not accepted by current models and are never sent.
        assert not {"temperature", "topP", "topK"} & generation.keys()
        sent_schema = json.dumps(generation["responseJsonSchema"])
        assert "$ref" not in sent_schema and "maxLength" not in sent_schema
    finally:
        await provider.close()


@pytest.mark.parametrize("schema", [AgentDecision, SafetySignal])
def test_wire_schema_keeps_structure_and_drops_unsupported_keywords(schema: type[Any]) -> None:
    source = schema.model_json_schema()
    wire = wire_schema(source)
    text = json.dumps(wire)
    assert "$ref" not in text and "$defs" not in text
    assert not {"maxLength", "minLength", "pattern", "default"} & set(
        key for key in text.replace('"', " ").split() if key.isalpha()
    )
    assert set(wire["properties"]) == set(source["properties"])
    assert wire["required"] == source["required"]
    assert wire["additionalProperties"] is False


def test_wire_schema_inlines_nested_models_and_enums() -> None:
    wire = wire_schema(AgentDecision.model_json_schema())
    assert "greeting" in wire["properties"]["intent"]["enum"]
    call = wire["properties"]["tool_calls"]["items"]
    assert set(call["properties"]) == {"name", "arguments_json"}
    assert wire["properties"]["tool_calls"]["maxItems"] == 3


OPENROUTER_FAILURES: dict[str, type[LLMProviderError]] = {
    "rate_limit": LLMRateLimitError,
    "bad_key": LLMCredentialsError,
    "no_credit": LLMCreditsError,
    "moderated": LLMRejectedError,
    "unknown_model": LLMRejectedError,
    "no_provider": LLMUnavailableError,
    "slow": LLMTimeoutError,
    "timeout": LLMTimeoutError,
    "offline": LLMUnavailableError,
    "filtered": LLMBlockedError,
    "truncated": LLMInvalidResponseError,
    "invalid": LLMInvalidResponseError,
    "empty": LLMInvalidResponseError,
    "failed_inside_200": LLMUnavailableError,
    "error_body": LLMUnavailableError,
}
OPENROUTER_STATUS = {
    "rate_limit": 429,
    "bad_key": 401,
    "no_credit": 402,
    "moderated": 403,
    "unknown_model": 404,
    "no_provider": 503,
    "slow": 408,
}


def openrouter_reply(outcome: str) -> httpx.Response:
    if outcome in OPENROUTER_STATUS:
        code = OPENROUTER_STATUS[outcome]
        return httpx.Response(code, json={"error": {"code": code, "message": "private-body"}})
    if outcome == "error_body":
        return httpx.Response(200, json={"error": {"code": 502, "message": "private-body"}})
    text: str | None = json.dumps({"intent": "greeting", "response": "Hello.", "tool_calls": []})
    finish = "stop"
    if outcome == "invalid":
        text = '{"intent":"unknown","response":"","tool_calls":[]}'
    elif outcome == "empty":
        text = None
    elif outcome == "filtered":
        finish = "content_filter"
    elif outcome == "truncated":
        finish = "length"
    choice: dict[str, Any] = {
        "finish_reason": finish,
        "message": {"role": "assistant", "content": text},
    }
    if outcome == "failed_inside_200":
        choice = {"finish_reason": "error", "error": {"code": 502, "message": "private-body"}}
    return httpx.Response(
        200,
        json={"id": "gen-1", "choices": [choice], "usage": {"prompt_tokens": 9, "cost": 0.00001}},
    )


@pytest.mark.parametrize("outcome", ["valid", *OPENROUTER_FAILURES])
async def test_openrouter_adapter_contract(outcome: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("private-timeout", request=request)
        if outcome == "offline":
            raise httpx.ConnectError("private-connect", request=request)
        return openrouter_reply(outcome)

    provider = OpenRouterLLMProvider(
        SecretStr("test-key"),
        "",
        1,
        800,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        if outcome == "valid":
            result = await provider.generate_structured(REQUEST, AgentDecision)
            assert result.response == "Hello."
        else:
            with pytest.raises(LLMProviderError) as error:
                await provider.generate_structured(REQUEST, AgentDecision)
            assert type(error.value) is OPENROUTER_FAILURES[outcome]
            assert "private" not in str(error.value)
    finally:
        await provider.close()
    # Exactly one billed request per call: a failure is never retried here.
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-key"
    body = json.loads(request.content)
    assert body["model"] == "google/gemini-3.8-flash"  # The default when none is configured.
    assert body["messages"] == [
        {"role": "system", "content": "Policy\n\nTools"},
        {"role": "user", "content": "Hello"},
    ]
    assert body["max_tokens"] == 800
    # Never an endpoint that keeps the conversation or ignores the schema; cheapest first.
    assert body["provider"] == {
        "data_collection": "deny",
        "zdr": True,
        "require_parameters": True,
        "sort": "price",
    }
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert not {"temperature", "top_p", "stream", "tools"} & body.keys()
    wire = body["response_format"]
    assert wire["type"] == "json_schema" and wire["json_schema"]["strict"] is True
    assert wire["json_schema"]["name"] == "AgentDecision"
    assert "$ref" not in json.dumps(wire) and "maxLength" not in json.dumps(wire)


def test_adapters_refuse_to_start_without_a_key() -> None:
    for adapter in (GeminiLLMProvider, OpenRouterLLMProvider):
        with pytest.raises(LLMCredentialsError):
            adapter(SecretStr(""), "model", 1, 800)
