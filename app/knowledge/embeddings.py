"""Embeddings use the same fixed model and dimension for indexing and queries."""

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import SecretStr

from app.config.settings import Settings

if TYPE_CHECKING:
    from app.integrations.resolver import TenantIntegrationResolver

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


class Embedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, settings: Settings, api_key: SecretStr | None = None) -> None:
        # A tenant connection supplies its own key; settings is the platform account.
        self.client = AsyncOpenAI(
            api_key=(api_key or settings.openai_api_key).get_secret_value(),
            timeout=10.0,
            max_retries=1,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or any(not text.strip() or len(text) > 6000 for text in texts):
            raise ValueError("Invalid embedding input")
        response = await self.client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [item.embedding for item in ordered]
        if len(vectors) != len(texts) or any(
            len(vector) != EMBEDDING_DIMENSIONS for vector in vectors
        ):
            raise ValueError("Embedding provider returned invalid dimensions")
        return vectors

    async def close(self) -> None:
        await self.client.close()


async def open_embedder(
    settings: Settings, resolver: "TenantIntegrationResolver | None", tenant_id: UUID
) -> OpenAIEmbedder:
    """Embedder for one tenant's request or job; the caller closes it.

    Uses the tenant's own embeddings connection. The platform key serves only tenants
    that are explicitly allowed to keep using platform provider accounts.
    """
    from app.integrations.resolver import IntegrationUnavailableError

    if resolver is None or not resolver.enabled:
        return OpenAIEmbedder(settings)
    connection = await resolver.active(tenant_id, "embeddings")
    if connection is not None:
        return OpenAIEmbedder(settings, connection.secret("api_key"))
    if resolver.legacy_allowed(tenant_id) and settings.openai_api_key.get_secret_value():
        return OpenAIEmbedder(settings)
    raise IntegrationUnavailableError()
