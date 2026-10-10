import random
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.appointments.schemas import AppointmentView
from app.events.outbox import EventType


class ProviderError(Exception):
    """Provider messages are private; only static codes may enter logs or outbox rows."""

    code = "provider_error"
    retryable = False


class TransientProviderError(ProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderTimeoutError(TransientProviderError):
    code = "provider_timeout"


class RateLimitedProviderError(TransientProviderError):
    code = "provider_rate_limited"

    def __init__(self, retry_after: float = 30) -> None:
        self.retry_after = max(0, min(retry_after, 3600))
        super().__init__()


class PermanentProviderError(ProviderError):
    code = "provider_rejected"


@dataclass(frozen=True)
class Notification:
    event_id: UUID
    tenant_id: UUID
    event_type: EventType
    appointment: AppointmentView


class NotificationProvider(Protocol):
    async def send(self, notification: Notification, *, idempotency_key: str) -> None:
        """Adapters must honor the stable event key at the downstream side-effect boundary."""
        ...


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_seconds: float = 2
    max_seconds: float = 300

    def delay(self, attempt: int, error: ProviderError) -> float | None:
        if not error.retryable or attempt >= self.max_attempts:
            return None
        ceiling = min(self.max_seconds, self.base_seconds * 2 ** min(max(attempt - 1, 0), 20))
        backoff = random.uniform(ceiling / 2, ceiling)
        if isinstance(error, RateLimitedProviderError):
            return max(backoff, error.retry_after)
        return backoff
