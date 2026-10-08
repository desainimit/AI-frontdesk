import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.agents.providers.base import (
    LLMBlockedError,
    LLMCredentialsError,
    LLMInvalidResponseError,
    LLMMessage,
    LLMProviderError,
    LLMRateLimitError,
    LLMRejectedError,
    LLMRequest,
    LLMTimeoutError,
    LLMUnavailableError,
)
from app.agents.providers.gemini import GeminiLLMProvider, wire_schema
from app.agents.schemas import AgentDecision
from app.safety.classifier import SafetySignal

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


async def test_gemini_plain_text_and_optional_thinking() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "Hi"}]},
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    provider = GeminiLLMProvider(
        SecretStr("test-key"),
        "test-model",
        1,
        800,
        thinking_level=None,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        assert (await provider.generate(REQUEST)).text == "Hi"
    finally:
        await provider.close()
    generation = seen[0]["generationConfig"]
    assert "thinkingConfig" not in generation and "responseJsonSchema" not in generation


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
