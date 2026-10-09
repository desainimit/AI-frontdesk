"""Embeddings use one fixed dimension for indexing and queries.

Vectors from different providers are not comparable. A deployment (or a tenant with its
own connection) must embed its documents and its questions with the same provider;
changing provider means re-indexing the documents.
"""

import asyncio
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast
from uuid import UUID

import httpx
from google.genai import Client, types
from pydantic import SecretStr

from app.config.settings import Settings

if TYPE_CHECKING:
    from app.integrations.resolver import TenantIntegrationResolver

EMBEDDING_MODEL = "gemini-embedding-2"
OPENROUTER_EMBEDDING_MODEL = "openai/text-embedding-3-small"
OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
EMBEDDING_DIMENSIONS = 1536
EmbeddingKind = Literal["document", "query"]
# The Gemini model is steered by these prefixes instead of a task-type parameter.
_PREFIX: dict[EmbeddingKind, str] = {
    "document": "title: none | text: ",
    "query": "task: search result | query: ",
}


class Embedder(Protocol):
    async def embed(
        self, texts: list[str], *, kind: EmbeddingKind = "document"
    ) -> list[list[float]]: ...


class OwnedEmbedder(Embedder, Protocol):
    """An embedder opened for one request or job; its opener closes it."""

    async def close(self) -> None: ...


def _checked(texts: list[str]) -> None:
    if not texts or any(not text.strip() or len(text) > 6000 for text in texts):
        raise ValueError("Invalid embedding input")


class GeminiEmbedder:
    def __init__(self, api_key: SecretStr, *, http_client: httpx.AsyncClient | None = None) -> None:
        if not api_key.get_secret_value():
            raise ValueError("Embedding credentials are not configured")
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self.client = Client(
            api_key=api_key.get_secret_value(),
            http_options=types.HttpOptions(
                httpx_async_client=self._http,
                timeout=10_000,
                retry_options=types.HttpRetryOptions(attempts=2),
            ),
        )
        self._limit = asyncio.Semaphore(4)

    async def _one(self, text: str) -> list[float]:
        # One text per request: this model returns a single combined vector when
        # several inputs share a request.
        async with self._limit:
            response = await self.client.aio.models.embed_content(
                model=EMBEDDING_MODEL,
                contents=text,
                config=types.EmbedContentConfig(output_dimensionality=EMBEDDING_DIMENSIONS),
            )
        embeddings = response.embeddings or []
        values = embeddings[0].values if len(embeddings) == 1 else None
        if values is None or len(values) != EMBEDDING_DIMENSIONS:
            raise ValueError("Embedding provider returned invalid dimensions")
        return list(values)

    async def embed(
        self, texts: list[str], *, kind: EmbeddingKind = "document"
    ) -> list[list[float]]:
        _checked(texts)
        return list(await asyncio.gather(*(self._one(_PREFIX[kind] + text) for text in texts)))

    async def close(self) -> None:
        await self.client.aio.aclose()
        self.client.close()
        await self._http.aclose()


class OpenRouterEmbedder:
    """One request per batch, never retried, and only to endpoints that keep no data."""

    def __init__(self, api_key: SecretStr, *, http_client: httpx.AsyncClient | None = None) -> None:
        if not api_key.get_secret_value():
            raise ValueError("Embedding credentials are not configured")
        self._key = api_key
        self._http = http_client or httpx.AsyncClient(timeout=10.0)

    async def embed(
        self, texts: list[str], *, kind: EmbeddingKind = "document"
    ) -> list[list[float]]:
        _checked(texts)
        response = await self._http.post(
            OPENROUTER_EMBEDDINGS_URL,
            json={
                "model": OPENROUTER_EMBEDDING_MODEL,
                "input": texts,
                "encoding_format": "float",
                "provider": {"data_collection": "deny"},
            },
            headers={"Authorization": f"Bearer {self._key.get_secret_value()}"},
        )
        if response.status_code != 200:
            # The body may quote the input; only a static message leaves this function.
            raise ValueError("Embedding provider rejected the request")
        rows = cast(list[dict[str, Any]], cast(dict[str, Any], response.json()).get("data") or [])
        ordered = sorted(rows, key=lambda row: int(row.get("index", 0)))
        vectors = [cast(list[float], row.get("embedding") or []) for row in ordered]
        if len(vectors) != len(texts) or any(
            len(vector) != EMBEDDING_DIMENSIONS for vector in vectors
        ):
            raise ValueError("Embedding provider returned invalid dimensions")
        return vectors

    async def close(self) -> None:
        await self._http.aclose()


def build_embedder(provider: str, api_key: SecretStr) -> OwnedEmbedder:
    if provider == "openrouter":
        return OpenRouterEmbedder(api_key)
    if provider == "gemini":
        return GeminiEmbedder(api_key)
    raise ValueError("Unsupported embeddings provider")


def platform_embedder(settings: Settings) -> OwnedEmbedder:
    """The platform account's embedder, following the selected model provider."""
    provider = "openrouter" if settings.llm_provider == "openrouter" else "gemini"
    return build_embedder(provider, settings.platform_llm_key)


async def open_embedder(
    settings: Settings, resolver: "TenantIntegrationResolver | None", tenant_id: UUID
) -> OwnedEmbedder:
    """Embedder for one tenant's request or job; the caller closes it.

    The tenant's own embeddings connection when it has one, otherwise the platform
    account. With neither, the request fails instead of borrowing another key.
    """
    from app.integrations.resolver import IntegrationUnavailableError

    connection = await resolver.active(tenant_id, "embeddings") if resolver else None
    if connection is not None:
        return build_embedder(connection.provider, connection.secret("api_key"))
    if settings.platform_llm_key.get_secret_value():
        return platform_embedder(settings)
    raise IntegrationUnavailableError()
