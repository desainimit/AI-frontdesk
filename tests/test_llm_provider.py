import json
from typing import Any

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from app.agents.providers.base import LLMMessage, LLMProviderError, LLMRequest
from app.agents.providers.openai import OpenAILLMProvider
from app.agents.schemas import AgentDecision


@pytest.mark.parametrize("outcome", ["valid", "refusal", "incomplete", "invalid", "rate_limit"])
async def test_openai_adapter_contract(outcome: str) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if outcome == "rate_limit":
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "private-provider-body",
                        "type": "rate_limit",
                        "code": "rate_limit",
                    }
                },
            )
        text = json.dumps({"intent": "greeting", "response": "Hello.", "tool_calls": []})
        content: dict[str, Any] = {"type": "output_text", "text": text, "annotations": []}
        if outcome == "refusal":
            content = {"type": "refusal", "refusal": "private refusal"}
        elif outcome == "invalid":
            content["text"] = '{"intent":"unknown","response":"","tool_calls":[]}'
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 0,
                "status": "incomplete" if outcome == "incomplete" else "completed",
                "model": "test-model",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [content],
                    }
                ],
                "incomplete_details": {"reason": "max_output_tokens"}
                if outcome == "incomplete"
                else None,
            },
        )

    client = AsyncOpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = OpenAILLMProvider(SecretStr("test-key"), "test-model", 1, 800, client=client)
    request = LLMRequest((LLMMessage("system", "Policy"), LLMMessage("user", "Hello")))
    try:
        if outcome == "valid":
            result = await provider.generate_structured(request, AgentDecision)
            assert result.response == "Hello."
        else:
            with pytest.raises(LLMProviderError) as error:
                await provider.generate_structured(request, AgentDecision)
            assert "private" not in str(error.value)
        assert len(seen) == 1
        assert seen[0]["store"] is False
        assert seen[0]["text"]["format"]["strict"] is True
        assert seen[0]["max_output_tokens"] == 800
    finally:
        await provider.close()
