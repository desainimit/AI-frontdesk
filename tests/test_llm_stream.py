import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.ai.llm.base import (
    AssistantTurn,
    ChatRequest,
    Completed,
    LLMCredentialsError,
    LLMInterruptedError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    LLMUnavailableError,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
    UserTurn,
)
from app.ai.llm.gateway import ModelGateway
from app.ai.llm.gemini import GeminiLLMProvider
from app.ai.llm.openrouter import OpenRouterLLMProvider
from app.voice.circuit import CircuitBreaker
from tests.fake_llm import ScriptedModel

TOOL = ToolSpec("list_providers", "The clinic's doctors.", {"type": "object", "properties": {}})
REQUEST = ChatRequest("Policy", (UserTurn("Which doctors do you have?"),), (TOOL,))


def sse(*events: dict[str, Any] | str, keep_alive: bool = True) -> httpx.Response:
    # OpenRouter sends comment lines while it is still working; Google does not.
    lines = [": keep-alive", ""] if keep_alive else []
    for event in events:
        lines += ["data: " + (event if isinstance(event, str) else json.dumps(event)), ""]
    return httpx.Response(200, text="\n".join(lines), headers={"content-type": "text/event-stream"})


async def collect(stream: Any) -> tuple[str, Completed]:
    text, last = "", None
    async for event in stream:
        if isinstance(event, TextDelta):
            text += event.text
        else:
            last = event
    assert isinstance(last, Completed)
    return text, last


def openrouter(handler: Any) -> OpenRouterLLMProvider:
    return OpenRouterLLMProvider(
        SecretStr("test-key"),
        "google/test-model",
        1,
        800,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_openrouter_streams_text_then_reports_what_it_cost() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer test-key"
        return sse(
            {"choices": [{"delta": {"content": "We have "}}]},
            {"choices": [{"delta": {"content": "two doctors."}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 6}},
            "[DONE]",
        )

    provider = openrouter(handler)
    text, done = await collect(provider.stream(REQUEST))
    await provider.close()
    assert text == done.turn.text == "We have two doctors."
    assert (done.finish, done.usage.input_tokens, done.usage.output_tokens) == ("stop", 40, 6)
    body = seen[0]
    assert body["stream"] is True and body["model"] == "google/test-model"
    assert body["messages"][0] == {"role": "system", "content": "Policy"}
    assert body["tools"][0]["function"]["name"] == "list_providers"
    # Nothing is routed to an endpoint that keeps or trains on the conversation.
    assert body["provider"]["data_collection"] == "deny"


async def test_openrouter_assembles_a_tool_call_and_sends_its_result_back() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            call = {"index": 0, "id": "call_9", "function": {"name": "list_providers"}}
            rest = {"index": 0, "function": {"arguments": "{}"}}
            return sse(
                {"choices": [{"delta": {"tool_calls": [call], "reasoning_details": [{"k": 1}]}}]},
                {"choices": [{"delta": {"tool_calls": [rest]}, "finish_reason": "tool_calls"}]},
                "[DONE]",
            )
        return sse({"choices": [{"delta": {"content": "Dr Rao."}, "finish_reason": "stop"}]})

    provider = openrouter(handler)
    _, first = await collect(provider.stream(REQUEST))
    assert first.finish == "tool_calls"
    assert first.turn.tool_calls == (ToolCall("call_9", "list_providers", {}),)
    result = ToolResult("call_9", "list_providers", {"items": [{"name": "Dr Rao"}]})
    text, _ = await collect(
        provider.stream(ChatRequest("Policy", (*REQUEST.turns, first.turn, result), (TOOL,)))
    )
    await provider.close()
    assert text == "Dr Rao."
    assistant, tool = bodies[1]["messages"][-2:]
    assert assistant["tool_calls"][0]["id"] == "call_9"
    # The model's own reasoning record goes back unchanged.
    assert assistant["reasoning_details"] == [{"k": 1}]
    assert tool == {
        "role": "tool",
        "tool_call_id": "call_9",
        "content": json.dumps({"items": [{"name": "Dr Rao"}]}),
    }


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (httpx.Response(429, json={"error": {"message": "private"}}), LLMRateLimitError),
        (httpx.Response(401, json={"error": {"message": "private"}}), LLMCredentialsError),
        (sse({"error": {"code": 502, "message": "private"}}), LLMUnavailableError),
        (sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}), LLMInvalidResponseError),
    ],
)
async def test_openrouter_stream_failures_are_typed(
    reply: httpx.Response, expected: type[LLMProviderError]
) -> None:
    provider = openrouter(lambda _: reply)
    with pytest.raises(LLMProviderError) as error:
        await collect(provider.stream(REQUEST))
    await provider.close()
    assert type(error.value) is expected and "private" not in str(error.value)


async def test_gemini_streams_text_and_function_calls() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        parts: list[dict[str, Any]] = [
            {"functionCall": {"name": "list_providers", "args": {}}, "thoughtSignature": "c2ln"}
        ]
        return sse(
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "One moment. "}]}}]},
            {
                "candidates": [
                    {"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 4},
            },
            keep_alive=False,
        )

    provider = GeminiLLMProvider(
        SecretStr("test-key"),
        "test-model",
        1,
        800,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    text, done = await collect(provider.stream(REQUEST))
    assert text == "One moment. " and done.finish == "tool_calls"
    assert [(call.name, call.arguments) for call in done.turn.tool_calls] == [
        ("list_providers", {})
    ]
    assert (done.usage.input_tokens, done.usage.output_tokens) == (30, 4)
    assert seen[0].url.path.endswith("/models/test-model:streamGenerateContent")
    body = json.loads(seen[0].content)
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "list_providers"
    # The next request shows the model its own parts again, signature included.
    result = ToolResult(done.turn.tool_calls[0].id, "list_providers", {"items": []})
    await collect(
        provider.stream(ChatRequest("Policy", (*REQUEST.turns, done.turn, result), (TOOL,)))
    )
    await provider.close()
    model_turn, tool_turn = json.loads(seen[1].content)["contents"][-2:]
    assert model_turn["parts"][-1]["thoughtSignature"] == "c2ln"
    assert tool_turn["parts"][0]["functionResponse"]["name"] == "list_providers"


class Flaky:
    """Fails a set number of times, optionally after saying something, then answers."""

    def __init__(self, failures: int, error: LLMProviderError, *, after_text: bool = False):
        self.failures, self.error, self.after_text, self.calls = failures, error, after_text, 0

    async def stream(self, request: ChatRequest) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            if self.after_text:
                yield TextDelta("Let me ")
            raise self.error
        yield TextDelta("Done.")
        yield Completed(AssistantTurn("Done."), "stop")

    async def generate_structured(self, request: Any, schema: Any) -> Any:
        raise NotImplementedError

    async def close(self) -> None:
        return None


def gateway(primary: Any, fallback: Any = None) -> ModelGateway:
    breaker = CircuitBreaker(name="test", failure_threshold=10, reset_seconds=60)
    return ModelGateway(primary, fallback=fallback, breaker=breaker, pause_seconds=0)


async def events(stream: Any) -> list[ModelEvent]:
    return [event async for event in stream]


async def test_gateway_retries_a_passing_failure_before_anything_was_said() -> None:
    model = Flaky(2, LLMRateLimitError("busy"))
    text, _ = await collect(gateway(model).stream(REQUEST))
    assert (text, model.calls) == ("Done.", 3)


async def test_gateway_gives_the_lighter_model_one_last_try() -> None:
    main, light = Flaky(9, LLMUnavailableError("down")), ScriptedModel("From the light model.")
    text, _ = await collect(gateway(main, light).stream(REQUEST))
    assert text == "From the light model." and main.calls == 3


async def test_gateway_never_replays_a_reply_that_was_partly_delivered() -> None:
    model = Flaky(1, LLMUnavailableError("down"), after_text=True)
    with pytest.raises(LLMInterruptedError):
        await events(gateway(model).stream(REQUEST))
    assert model.calls == 1


async def test_gateway_does_not_retry_a_refused_key_and_remembers_why() -> None:
    model = Flaky(9, LLMCredentialsError("refused"))
    door = gateway(model)
    with pytest.raises(LLMCredentialsError):
        await events(door.stream(REQUEST))
    assert model.calls == 1 and door.account_problem == "llm_credentials_rejected"


async def test_gateway_stops_asking_once_the_circuit_is_open() -> None:
    breaker = CircuitBreaker(name="test", failure_threshold=1, reset_seconds=60)
    model = Flaky(9, LLMUnavailableError("down"))
    door = ModelGateway(model, breaker=breaker, pause_seconds=0)
    with pytest.raises(LLMUnavailableError):
        await events(door.stream(REQUEST))
    with pytest.raises(LLMUnavailableError):
        await events(door.stream(REQUEST))
    # The second turn never reached the model.
    assert model.calls == 1
