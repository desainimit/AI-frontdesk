from dataclasses import dataclass
from enum import StrEnum
from time import monotonic


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Bounded, time-windowed circuit breaker for external voice providers."""

    name: str
    failure_threshold: int = 3
    reset_seconds: float = 60.0
    _consecutive_failures: int = 0
    _opened_at: float = 0.0
    _state: CircuitState = CircuitState.CLOSED
    _probe_started: float | None = None

    @property
    def state(self) -> CircuitState:
        if (
            self._state == CircuitState.OPEN
            and (monotonic() - self._opened_at) >= self.reset_seconds
        ):
            self._state = CircuitState.HALF_OPEN
        return self._state

    def allow_request(self) -> bool:
        """Determines whether a trial or normal request can proceed."""
        current = self.state
        if current == CircuitState.HALF_OPEN:
            now = monotonic()
            if self._probe_started is not None and now - self._probe_started < self.reset_seconds:
                return False
            self._probe_started = now
            return True
        return current == CircuitState.CLOSED

    def record_success(self) -> None:
        """Call succeeded: reset failure counter and close circuit."""
        self._consecutive_failures = 0
        self._probe_started = None
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        """Call failed: increment failures and open circuit if threshold exceeded."""
        self._probe_started = None
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = monotonic()

    def reset(self) -> None:
        """Explicitly reset state (e.g. for testing)."""
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._probe_started = None
        self._state = CircuitState.CLOSED


class CircuitRegistry:
    """Breakers keyed by tenant connection, so one tenant's failures stay its own."""

    def __init__(self, max_entries: int = 2048) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._max_entries = max_entries

    def get(self, key: str, failure_threshold: int, reset_seconds: float) -> CircuitBreaker:
        breaker = self._breakers.pop(key, None)
        if breaker is None:
            if len(self._breakers) >= self._max_entries:
                # Least recently used first; a rotated credential gets a fresh key anyway.
                self._breakers.pop(next(iter(self._breakers)))
            breaker = CircuitBreaker(
                name=key, failure_threshold=failure_threshold, reset_seconds=reset_seconds
            )
        self._breakers[key] = breaker
        return breaker
