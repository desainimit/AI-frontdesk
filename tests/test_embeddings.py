import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.knowledge.embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    GeminiEmbedder,
    OpenRouterEmbedder,
    platform_embedder,
)


def gemini(handler: Any) -> GeminiEmbedder:
    return GeminiEmbedder(
        SecretStr("test-key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def openrouter(handler: Any) -> OpenRouterEmbedder:
    return OpenRouterEmbedder(
        SecretStr("test-key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def vector(marker: float, size: int = EMBEDDING_DIMENSIONS) -> list[float]:
    return [marker] + [0.0] * (size - 1)


async def test_gemini_embeds_each_text_separately_in_input_order() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "test-key"
        assert f"/models/{EMBEDDING_MODEL}:" in request.url.path
        body = json.loads(request.content)
        seen.append(body)
        text = body["requests"][0]["content"]["parts"][0]["text"]
        # The first value identifies which text this vector belongs to.
        return httpx.Response(
            200, json={"embeddings": [{"values": vector(2.0 if text.endswith("beta") else 1.0)}]}
        )

    provider = gemini(handler)
    try:
        vectors = await provider.embed(["alpha", "beta"])
        query = await provider.embed(["opening hours"], kind="query")
    finally:
        await provider.close()
    assert [item[0] for item in vectors] == [1.0, 2.0]
    assert all(len(item) == EMBEDDING_DIMENSIONS for item in [*vectors, *query])
    # One text per request: several inputs in one request would be merged into one vector.
    assert all(len(body["requests"]) == 1 for body in seen)
    assert all(body["requests"][0]["outputDimensionality"] == EMBEDDING_DIMENSIONS for body in seen)
    texts = sorted(body["requests"][0]["content"]["parts"][0]["text"] for body in seen)
    assert texts == [
        "task: search result | query: opening hours",
        "title: none | text: alpha",
        "title: none | text: beta",
    ]


async def test_openrouter_embeds_a_batch_in_one_request_and_restores_input_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # Returned out of order on purpose; the index says which input each belongs to.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": vector(2.0)},
                    {"index": 0, "embedding": vector(1.0)},
                ]
            },
        )

    provider = openrouter(handler)
    try:
        vectors = await provider.embed(["alpha", "beta"])
    finally:
        await provider.close()
    assert [item[0] for item in vectors] == [1.0, 2.0]
    assert len(seen) == 1  # One billed request for the whole batch.
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(seen[0].content) == {
        "model": "openai/text-embedding-3-small",
        "input": ["alpha", "beta"],
        "encoding_format": "float",
        "provider": {"data_collection": "deny"},
    }


@pytest.mark.parametrize("kind", ["gemini", "openrouter"])
async def test_wrong_dimensions_and_invalid_input_are_rejected(kind: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        short = [0.1] * 8
        return httpx.Response(
            200,
            json={"embeddings": [{"values": short}], "data": [{"index": 0, "embedding": short}]},
        )

    provider = gemini(handler) if kind == "gemini" else openrouter(handler)
    try:
        with pytest.raises(ValueError, match="invalid dimensions"):
            await provider.embed(["alpha"])
        assert calls == 1
        for invalid in ([], [" "], ["x" * 6001]):
            with pytest.raises(ValueError, match="Invalid embedding input"):
                await provider.embed(invalid)
        assert calls == 1  # Rejected before any paid request.
    finally:
        await provider.close()


async def test_openrouter_rejection_is_reported_without_the_provider_body() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(402, json={"error": {"code": 402, "message": "private-body"}})

    provider = openrouter(handler)
    try:
        with pytest.raises(ValueError) as error:
            await provider.embed(["alpha"])
    finally:
        await provider.close()
    assert "private" not in str(error.value) and calls == 1


def test_missing_key_fails_with_a_static_message(settings: Settings) -> None:
    for embedder in (GeminiEmbedder, OpenRouterEmbedder):
        with pytest.raises(ValueError, match="not configured"):
            embedder(SecretStr(""))
    assert not settings.platform_llm_key.get_secret_value()
    with pytest.raises(ValueError, match="not configured"):
        platform_embedder(settings)


def test_platform_embedder_follows_the_selected_provider(settings: Settings) -> None:
    chosen = settings.model_copy(
        update={"llm_provider": "openrouter", "openrouter_api_key": SecretStr("test-key")}
    )
    assert isinstance(platform_embedder(chosen), OpenRouterEmbedder)
    direct = settings.model_copy(
        update={"llm_provider": "gemini", "gemini_api_key": SecretStr("test-key")}
    )
    assert isinstance(platform_embedder(direct), GeminiEmbedder)
