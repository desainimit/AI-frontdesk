import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.knowledge.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, GeminiEmbedder


def embedder(settings: Settings, handler: Any) -> GeminiEmbedder:
    return GeminiEmbedder(
        settings,
        SecretStr("test-key"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_each_text_is_embedded_separately_in_input_order(settings: Settings) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "test-key"
        assert f"/models/{EMBEDDING_MODEL}:" in request.url.path
        body = json.loads(request.content)
        seen.append(body)
        text = body["requests"][0]["content"]["parts"][0]["text"]
        # The first value identifies which text this vector belongs to.
        marker = 2.0 if text.endswith("beta") else 1.0
        return httpx.Response(
            200, json={"embeddings": [{"values": [marker] + [0.0] * (EMBEDDING_DIMENSIONS - 1)}]}
        )

    provider = embedder(settings, handler)
    try:
        vectors = await provider.embed(["alpha", "beta"])
        query = await provider.embed(["opening hours"], kind="query")
    finally:
        await provider.close()
    assert [vector[0] for vector in vectors] == [1.0, 2.0]
    assert all(len(vector) == EMBEDDING_DIMENSIONS for vector in [*vectors, *query])
    # One text per request: several inputs in one request would be merged into one vector.
    assert all(len(body["requests"]) == 1 for body in seen)
    assert all(body["requests"][0]["outputDimensionality"] == EMBEDDING_DIMENSIONS for body in seen)
    texts = sorted(body["requests"][0]["content"]["parts"][0]["text"] for body in seen)
    assert texts == [
        "task: search result | query: opening hours",
        "title: none | text: alpha",
        "title: none | text: beta",
    ]


async def test_wrong_dimensions_and_invalid_input_are_rejected(settings: Settings) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"embeddings": [{"values": [0.1] * 8}]})

    provider = embedder(settings, handler)
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


def test_missing_key_fails_with_a_static_message(settings: Settings) -> None:
    assert not settings.gemini_api_key.get_secret_value()
    with pytest.raises(ValueError, match="not configured"):
        GeminiEmbedder(settings)
