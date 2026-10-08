from app.cache.redis import RedisClient
from app.config.settings import Environment, Settings
from app.conversation.store import (
    ConversationStateStore,
    MemoryConversationStateStore,
    RedisConversationStateStore,
)
from app.resources import Dependency


def build_conversation_store(
    settings: Settings,
    redis: Dependency | None = None,
) -> ConversationStateStore:
    """Builds conversation state storage, utilizing Redis if provided or falling back to Memory."""
    if isinstance(redis, RedisClient):
        return RedisConversationStateStore(redis.client)
    if settings.environment not in {Environment.LOCAL, Environment.DEVELOPMENT}:
        raise RuntimeError("Redis is required for conversation state in this environment")
    return MemoryConversationStateStore()
