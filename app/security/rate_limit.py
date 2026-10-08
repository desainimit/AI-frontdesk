"""Atomic, shared Redis limits keyed only from verified identities."""

import hashlib
from collections.abc import Awaitable
from typing import cast

from redis.exceptions import RedisError

from app.cache.redis import RedisClient
from app.errors import ApplicationError


class RateLimitExceeded(ApplicationError):
    status_code = 429
    code = "rate_limit_exceeded"
    public_message = "Too many requests. Try again later."


class RateLimitUnavailable(ApplicationError):
    status_code = 503
    code = "rate_limit_unavailable"
    public_message = "Request protection is temporarily unavailable."


_INCREMENT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


async def enforce_rate_limit(
    redis: object,
    *,
    enabled: bool,
    scope: str,
    identity: str,
    limit: int,
    window_seconds: int = 60,
) -> None:
    if not enabled:
        return
    if not isinstance(redis, RedisClient):
        raise RateLimitUnavailable()
    digest = hashlib.sha256(identity.encode()).hexdigest()
    key = f"frontdesk:limit:{scope}:{digest}"
    try:
        result = await cast(
            Awaitable[object],
            redis.client.eval(_INCREMENT, 1, key, str(window_seconds)),  # pyright: ignore[reportUnknownMemberType]
        )
    except RedisError:
        raise RateLimitUnavailable() from None
    if not isinstance(result, int):
        raise RateLimitUnavailable()
    if result > limit:
        raise RateLimitExceeded()
