import asyncio
import logging
from time import perf_counter

from pydantic import ValidationError

from app.appointments.schemas import AppointmentView
from app.events.outbox import EventType
from app.events.providers import (
    Notification,
    NotificationProvider,
    PermanentProviderError,
    ProviderError,
    ProviderTimeoutError,
    RetryPolicy,
    TransientProviderError,
)
from app.events.store import OutboxStore
from app.observability.tracing import Telemetry

logger = logging.getLogger(__name__)


class EventProcessor:
    def __init__(
        self,
        store: OutboxStore,
        provider: NotificationProvider,
        *,
        retry: RetryPolicy | None = None,
        timeout_seconds: float = 10,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._store, self._provider = store, provider
        self._retry, self._timeout = retry or RetryPolicy(), timeout_seconds
        self._attempts = (
            telemetry.meter.create_counter("outbox.delivery.attempts") if telemetry else None
        )
        self._duration = (
            telemetry.meter.create_histogram("outbox.delivery.duration", unit="s")
            if telemetry
            else None
        )

    async def process_one(self) -> bool:
        event = await self._store.claim()
        if event is None:
            return False
        started = perf_counter()
        failure: ProviderError | None = None
        try:
            if event.attempts > self._retry.max_attempts:
                raise PermanentProviderError()
            if event.schema_version != 1:
                raise PermanentProviderError()
            appointment = AppointmentView.model_validate(event.payload)
            if appointment.tenant_id != event.tenant_id:
                raise PermanentProviderError()
            notification = Notification(
                event.id, event.tenant_id, EventType(event.event_type), appointment
            )
            async with asyncio.timeout(self._timeout):
                await self._provider.send(notification, idempotency_key=str(event.id))
        except TimeoutError:
            failure = ProviderTimeoutError()
        except ProviderError as exc:
            failure = exc
        except (ValidationError, ValueError):
            failure = PermanentProviderError()
        except Exception as exc:
            logger.error(
                "outbox_provider_unexpected_error", extra={"error_type": type(exc).__name__}
            )
            failure = TransientProviderError()
        delay = self._retry.delay(event.attempts, failure) if failure else None
        completed = await self._store.finish(
            event, error=failure.code if failure else None, retry_in=delay
        )
        outcome = (
            "lease_lost"
            if not completed
            else ("delivered" if failure is None else ("dead" if delay is None else "retry"))
        )
        logger.info(
            "outbox_delivery_attempt",
            extra={
                "event_id": str(event.id),
                "tenant_id": str(event.tenant_id),
                "outcome": outcome,
            },
        )
        if self._attempts:
            self._attempts.add(1, {"outcome": outcome})
        if self._duration:
            self._duration.record(perf_counter() - started, {"outcome": outcome})
        return True
