from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.config.settings import Settings

MAX_CONNECTIONS = 20


class RedisClient:
    def __init__(self, settings: Settings) -> None:
        # redis-py leaves **kwargs untyped in these two SDK methods.
        self.client: Redis = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=settings.dependency_timeout_seconds,
            socket_timeout=settings.dependency_timeout_seconds,
            max_connections=MAX_CONNECTIONS,
            retry=Retry(NoBackoff(), 0),
        )

    async def ping(self) -> None:
        if not await self.client.ping():  # pyright: ignore[reportUnknownMemberType]
            raise ConnectionError("Redis ping failed")

    async def close(self) -> None:
        await self.client.aclose(close_connection_pool=True)
