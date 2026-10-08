import pytest

from app.events.providers import (
    PermanentProviderError,
    RateLimitedProviderError,
    RetryPolicy,
    TransientProviderError,
)


def test_retry_limits_backoff_and_rate_limit_hint() -> None:
    policy = RetryPolicy(max_attempts=5, base_seconds=2, max_seconds=10)
    for attempt, bounds in ((1, (1, 2)), (2, (2, 4)), (4, (5, 10))):
        delay = policy.delay(attempt, TransientProviderError())
        assert delay is not None and bounds[0] <= delay <= bounds[1]
    assert policy.delay(5, TransientProviderError()) is None
    assert policy.delay(1, PermanentProviderError()) is None
    assert policy.delay(1, RateLimitedProviderError(60)) == 60


@pytest.mark.parametrize("hint,expected", [(-10, 0), (100000, 3600)])
def test_provider_retry_hint_is_bounded(hint: float, expected: float) -> None:
    assert RateLimitedProviderError(hint).retry_after == expected
