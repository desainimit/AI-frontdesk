"""Choosing a model per job, and sending each model only what it accepts."""

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from app.ai.llm.base import (
    AssistantTurn,
    ChatRequest,
    LLMMessage,
    LLMRejectedError,
    LLMRequest,
    ToolCall,
    ToolResult,
    UsageMeter,
    UserTurn,
)
from app.ai.llm.factory import open_models
from app.ai.llm.models import DEFAULTS, MODELS, ModelChoiceError, model_jobs, profile
from app.ai.llm.openrouter import OpenRouterLLMProvider
from tests.test_llm_stream import collect, sse

REQUEST = ChatRequest(
    "Policy",
    (UserTurn("Which doctors do you have?"),),
    context="Where things stand",
    session_id="conversation-1",
)


def provider(model: str, handler: Any, **options: Any) -> OpenRouterLLMProvider:
    return OpenRouterLLMProvider(
        SecretStr("test-key"),
        model,
        1,
        800,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **options,
    )


def answering(seen: list[dict[str, Any]], usage: dict[str, Any] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return sse(
            {"provider": "Google Vertex", "choices": [{"delta": {"content": "Two."}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2}},
            "[DONE]",
        )

    return handler


def test_every_catalog_model_has_a_default_set_that_validates() -> None:
    for name in DEFAULTS:
        jobs = model_jobs(name, {})
        assert jobs.backup.slug not in {jobs.text.slug, jobs.voice.slug}
    # Every model in the catalog does some job, and every price is set.
    assert all(model.jobs and model.price_in > 0 and model.price_out > 0 for model in MODELS)


@pytest.mark.parametrize(
    ("config", "field"),
    [
        ({"voice_model": "google/gemini-3.8-flash"}, "voice_model"),  # Too slow for calls.
        ({"model": "qwen/qwen3.8-flash"}, "model"),  # No zero-retention endpoint.
        ({"model": "gemini-3.8-flash"}, "model"),  # Google's own name, not OpenRouter's.
        ({"backup_model": "google/gemini-3.1-flash-lite"}, "backup_model"),  # Same company.
        ({"backup_model": "google/gemini-3.5-flash-lite"}, "backup_model"),  # Same model.
    ],
)
def test_a_choice_the_catalog_does_not_allow_says_which_job(
    config: dict[str, str], field: str
) -> None:
    with pytest.raises(ModelChoiceError) as refused:
        model_jobs("openrouter", config)
    assert refused.value.field == field


def test_a_withdrawn_model_falls_back_to_the_defaults_instead_of_no_receptionist() -> None:
    models = open_models("openrouter", SecretStr("k"), {"model": "x/gone"}, "a", voice=False)
    assert models.reply.model == DEFAULTS["openrouter"]["text"]  # type: ignore[attr-defined]


def test_on_a_call_the_backup_comes_from_another_company() -> None:
    models = open_models("openrouter", SecretStr("k"), {}, "platform", voice=True)
    reply, backup = profile(models.reply.model), profile(models.backup.model)  # type: ignore[attr-defined]
    assert reply is not None and backup is not None and reply.maker != backup.maker


async def test_claude_is_asked_to_cache_the_fixed_instructions_and_thinks_least() -> None:
    seen: list[dict[str, Any]] = []
    model = provider("anthropic/claude-haiku-5.5", answering(seen), reasoning_effort="none")
    await collect(model.stream(REQUEST))
    body = seen[0]
    fixed, changing = body["messages"][0], body["messages"][1]
    assert fixed["content"] == [
        {"type": "text", "text": "Policy", "cache_control": {"type": "ephemeral"}}
    ]
    # What changes every turn comes after the cached part and is not cached.
    assert changing == {"role": "system", "content": "Where things stand"}
    assert body["reasoning"] == {"effort": "none"}
    assert body["session_id"] == "conversation-1"
    assert body["provider"]["zdr"] is True and body["provider"]["data_collection"] == "deny"


async def test_gemini_caches_by_itself_and_is_not_marked() -> None:
    seen: list[dict[str, Any]] = []
    await collect(provider("google/gemini-3.5-flash-lite", answering(seen)).stream(REQUEST))
    assert "cache_control" not in seen[0]["messages"][0]["content"][0]


@pytest.mark.parametrize(
    ("model", "priority"),
    [("google/gemini-3.5-flash-lite", True), ("anthropic/claude-haiku-5.5", False)],
)
async def test_the_priority_lane_is_asked_for_only_where_the_model_has_one(
    model: str, priority: bool
) -> None:
    seen: list[dict[str, Any]] = []
    await collect(provider(model, answering(seen), fastest=True).stream(REQUEST))
    assert ("service_tier" in seen[0]) is priority
    assert seen[0]["provider"]["sort"] == "latency"


async def test_one_models_reasoning_record_is_never_shown_to_another() -> None:
    seen: list[dict[str, Any]] = []
    call = ToolCall("call_1", "list_providers", {})
    earlier = AssistantTurn("", (call,), [{"type": "reasoning.text"}], "google/gemini-3.8-flash")
    request = ChatRequest(
        "Policy", (UserTurn("Hi"), earlier, ToolResult("call_1", "list_providers", {"items": []}))
    )
    await collect(provider("anthropic/claude-haiku-5.5", answering(seen)).stream(request))
    assistant = next(m for m in seen[0]["messages"] if m["role"] == "assistant")
    assert "reasoning_details" not in assistant and assistant["tool_calls"]
    seen.clear()
    await collect(provider("google/gemini-3.8-flash", answering(seen)).stream(request))
    assistant = next(m for m in seen[0]["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_details"] == [{"type": "reasoning.text"}]


async def test_the_cost_reported_by_openrouter_is_kept_with_cached_tokens_and_endpoint() -> None:
    usage = {
        "prompt_tokens": 9000,
        "completion_tokens": 100,
        "prompt_tokens_details": {"cached_tokens": 8000},
        "cost": 0.00123,
    }
    _, done = await collect(
        provider("google/gemini-3.5-flash-lite", answering([], usage)).stream(REQUEST)
    )
    assert (done.usage.cached_tokens, done.usage.cost_usd) == (8000, 0.00123)
    assert done.served_by == "Google Vertex"


async def test_without_a_reported_cost_the_catalog_price_is_used() -> None:
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    _, done = await collect(
        provider("anthropic/claude-haiku-5.5", answering([], usage)).stream(REQUEST)
    )
    assert done.usage.cost_usd == pytest.approx(0.10)


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


async def test_a_quick_check_reports_its_cost_to_the_conversation() -> None:
    meter = UsageMeter()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": '{"answer": "yes"}'}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.0001},
            },
        )

    quick = provider("openai/gpt-6-luna", handler, meter=meter)
    request = LLMRequest((LLMMessage("system", "Read it"), LLMMessage("user", "yes")))
    assert (await quick.generate_structured(request, Reading)).answer == "yes"
    taken = meter.take()
    assert (taken.input_tokens, taken.cost_usd) == (50, 0.0001)
    assert meter.take().cost_usd == 0


async def test_no_endpoint_left_for_a_model_is_a_refusal_not_a_retry() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "No endpoints found"}})

    with pytest.raises(LLMRejectedError) as refused:
        await collect(provider("anthropic/claude-haiku-5.5", handler).stream(REQUEST))
    assert not refused.value.retryable
