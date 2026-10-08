from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache.redis import RedisClient
from app.config.settings import Settings
from app.maintenance.backup import verify_restore
from app.maintenance.load_probe import probe
from app.security.rate_limit import RateLimitExceeded, RateLimitUnavailable, enforce_rate_limit


class Counter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.fail = False

    async def eval(self, _script: str, _keys: int, key: str, _ttl: str) -> int:
        if self.fail:
            raise RedisError("injected outage")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]


class StubRedis(RedisClient):
    def __init__(self, counter: Counter) -> None:
        self.client = cast(Redis, counter)


async def test_rate_limit_is_atomic_per_verified_identity_and_fails_closed() -> None:
    counter = Counter()
    redis = StubRedis(counter)
    for _ in range(2):
        await enforce_rate_limit(
            redis, enabled=True, scope="tenant_api", identity="tenant:user-a", limit=2
        )
    with pytest.raises(RateLimitExceeded):
        await enforce_rate_limit(
            redis, enabled=True, scope="tenant_api", identity="tenant:user-a", limit=2
        )
    await enforce_rate_limit(
        redis, enabled=True, scope="tenant_api", identity="tenant:user-b", limit=2
    )
    assert all("user-a" not in key for key in counter.counts)
    counter.fail = True
    with pytest.raises(RateLimitUnavailable):
        await enforce_rate_limit(
            redis, enabled=True, scope="tenant_api", identity="tenant:user-b", limit=2
        )
    await enforce_rate_limit(
        object(), enabled=False, scope="tenant_api", identity="unused", limit=2
    )


def test_production_requires_rate_limits(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {**settings.model_dump(), "environment": "production", "rate_limits_enabled": False}
        )


async def test_probe_reports_request_failures() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200 if calls < 3 else 503, request=request)

    result = await probe(
        "http://localhost/health/live",
        requests=3,
        concurrency=1,
        transport=httpx.MockTransport(handler),
    )
    assert result.succeeded == 2
    assert result.failed == 1
    assert result.p95_ms >= result.p50_ms >= 0


def test_restore_rejects_non_scratch_target_before_accessing_archive(settings: Settings) -> None:
    with pytest.raises(ValueError, match="frontdesk_restore_test"):
        verify_restore(settings, Path("missing.dump"), "frontdesk")
