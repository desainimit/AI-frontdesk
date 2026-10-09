import hashlib
from collections.abc import Awaitable
from typing import cast

from redis.exceptions import RedisError

from app.core.errors import ApplicationError
from app.core.redis import RedisClient


class LoginThrottledError(ApplicationError):
    status_code = 429
    code = "login_throttled"
    public_message = "Too many login attempts. Try again later."


class AuthenticationUnavailableError(ApplicationError):
    status_code = 503
    code = "authentication_unavailable"
    public_message = "Authentication is temporarily unavailable."


async def check_login_limit(
    redis: RedisClient, email: str, address: str, *, scope: str = "platform-login"
) -> None:
    # Bounded counters with atomic TTLs, shared across API replicas. No PII in keys.
    script = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then redis.call('EXPIRE', KEYS[1], 300) end
    return count
    """
    try:
        for kind, value, limit in (("address", address, 20), ("account", email, 5)):
            key = f"{scope}:{kind}:{hashlib.sha256(value.encode()).hexdigest()}"
            result = cast(Awaitable[object], redis.client.eval(script, 1, key))  # pyright: ignore[reportUnknownMemberType]
            count = await result
            if not isinstance(count, int):
                raise AuthenticationUnavailableError()
            if count > limit:
                raise LoginThrottledError()
    except RedisError:
        raise AuthenticationUnavailableError() from None
