"""Embeddings use the same fixed model and dimension for indexing and queries."""

import asyncio
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID

import httpx
from google.genai import Client, types
from pydantic import SecretStr

from app.config.settings import Settings

if TYPE_CHECKING:
    from app.integrations.resolver import TenantIntegrationResolver

EMBEDDING_MODEL = "gemini-embedding-2"
EMBEDDING_DIMENSIONS = 1536
EmbeddingKind = Literal["document", "query"]
# The model is steered by these prefixes instead of a task-type parameter.
_PREFIX: dict[EmbeddingKind, str] = {
    "document": "title: none | text: ",
    "query": "task: search result | query: ",
}


class Embedder(Protocol):
    async def embed(
        self, texts: list[str], *, kind: EmbeddingKind = "document"
    ) -> list[list[float]]: ...


class GeminiEmbedder:
    def __init__(
        self,
        settings: Settings,
        api_key: SecretStr | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # A tenant connection supplies its own key; settings is the platform account.
        secret = (api_key or settings.gemini_api_key).get_secret_value()
        if not secret:
            raise ValueError("Embedding credentials are not configured")
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self.client = Client(
            api_key=secret,
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
        if not texts or any(not text.strip() or len(text) > 6000 for text in texts):
            raise ValueError("Invalid embedding input")
        return list(await asyncio.gather(*(self._one(_PREFIX[kind] + text) for text in texts)))

    async def close(self) -> None:
        await self.client.aio.aclose()
        self.client.close()
        await self._http.aclose()


async def open_embedder(
    settings: Settings, resolver: "TenantIntegrationResolver | None", tenant_id: UUID
) -> GeminiEmbedder:
    """Embedder for one tenant's request or job; the caller closes it.

    Uses the tenant's own embeddings connection. The platform key serves only tenants
    that are explicitly allowed to keep using platform provider accounts.
    """
    from app.integrations.resolver import IntegrationUnavailableError

    if resolver is None or not resolver.enabled:
        return GeminiEmbedder(settings)
    connection = await resolver.active(tenant_id, "embeddings")
    if connection is not None:
        return GeminiEmbedder(settings, connection.secret("api_key"))
    if resolver.legacy_allowed(tenant_id) and settings.gemini_api_key.get_secret_value():
        return GeminiEmbedder(settings)
    raise IntegrationUnavailableError()
