"""Small, replaceable contracts for external systems.

Implementations must deduplicate the stable idempotency key before side effects.
They raise the typed errors from ``app.events.providers`` for worker retry policy.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.events.outbox import EventType


@dataclass(frozen=True)
class CalendarChange:
    event_id: UUID
    tenant_id: UUID
    appointment_id: UUID
    external_uid: str
    provider_id: UUID
    event_type: EventType
    version: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class CustomerAppointmentChange:
    event_id: UUID
    tenant_id: UUID
    appointment_id: UUID
    patient_id: UUID
    provider_id: UUID
    event_type: EventType
    version: int
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class EmailMessage:
    tenant_id: UUID
    delivery_id: UUID
    recipient: str
    subject: str
    body: str


@dataclass(frozen=True)
class SMSMessage:
    tenant_id: UUID
    delivery_id: UUID
    recipient: str
    body: str


class CalendarProvider(Protocol):
    async def apply_appointment(self, change: CalendarChange, *, idempotency_key: str) -> None: ...


class CRMProvider(Protocol):
    async def apply_appointment(
        self, change: CustomerAppointmentChange, *, idempotency_key: str
    ) -> None: ...


class EHRProvider(Protocol):
    async def apply_appointment(
        self, change: CustomerAppointmentChange, *, idempotency_key: str
    ) -> None: ...


class EmailProvider(Protocol):
    async def send(self, message: EmailMessage, *, idempotency_key: str) -> None: ...


class SMSProvider(Protocol):
    async def send(self, message: SMSMessage, *, idempotency_key: str) -> None: ...
