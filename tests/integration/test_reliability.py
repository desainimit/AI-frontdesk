import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.appointments.commands import CancelAppointment, RescheduleAppointment
from app.appointments.models import Appointment
from app.appointments.service import AppointmentService
from app.audit.events import AuditAction
from app.database.rls import UnsafeDatabaseRoleError, set_tenant_scope
from app.database.session import Database
from app.events.idempotency import IdempotencyConflictError
from app.events.models import IdempotencyRecord, OutboxEvent
from app.events.outbox import EventType, OutboxRepository
from app.events.processor import EventProcessor
from app.events.providers import (
    Notification,
    PermanentProviderError,
    RetryPolicy,
    TransientProviderError,
)
from app.events.store import OutboxStore
from app.main import create_app
from app.patients.models import Patient
from tests.fakes import FakeDependency
from tests.integration.test_domain import DomainFixture, events, seed
from tests.integration.test_domain import domain as domain
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


class RecordingProvider:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.keys: list[str] = []
        self.accepted: set[str] = set()

    async def send(self, notification: Notification, *, idempotency_key: str) -> None:
        self.keys.append(idempotency_key)
        if self.failures:
            self.failures -= 1
            raise TransientProviderError("secret provider response must not be persisted")
        self.accepted.add(
            idempotency_key
        )  # Models downstream idempotency, including crash recovery.


@pytest.fixture
async def worker(tenancy: TenancyFixture, domain: DomainFixture) -> AsyncGenerator[Database]:
    assert tenancy.settings.worker_database_url is not None, "Set disposable WORKER_DATABASE_URL"
    # Prior tests leave events in this disposable DB; isolate the queue for each worker test.
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(OutboxEvent).values(status="dead", lease_token=None, lease_until=None)
        )
    database = Database(
        tenancy.settings.model_copy(
            update={"database_url": tenancy.settings.worker_database_url, "db_pool_size": 3}
        )
    )
    try:
        yield database
    finally:
        await database.close()


async def outbox(tenancy: TenancyFixture, domain: DomainFixture) -> list[OutboxEvent]:
    async with tenancy.admin.begin() as session:
        return list(
            (
                await session.scalars(
                    select(OutboxEvent)
                    .where(OutboxEvent.tenant_id == domain.context.tenant_id)
                    .order_by(OutboxEvent.aggregate_version)
                )
            ).all()
        )


async def make_due(tenancy: TenancyFixture, domain: DomainFixture) -> None:
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.tenant_id == domain.context.tenant_id)
            .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def test_concurrent_idempotency_replays_one_booking_audit_and_event(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    service = AppointmentService(domain.uow)
    first, second = await asyncio.gather(
        service.book(domain.context, domain.booking(), "booking-1"),
        service.book(domain.context, domain.booking(), "booking-1"),
    )
    assert first == second
    assert len(await service.list(domain.context, 100, 0)) == 1
    assert len(await events(domain, AuditAction.APPOINTMENT_CREATED)) == 1
    assert len(await outbox(tenancy, domain)) == 1
    with pytest.raises(IdempotencyConflictError):
        await service.book(domain.context, domain.booking(30), "booking-1")


async def test_cancel_reschedule_and_original_booking_replay(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    service = AppointmentService(domain.uow)
    booked = await service.book(domain.context, domain.booking(), "book")
    move = RescheduleAppointment(starts_at=domain.start + timedelta(minutes=30), expected_version=1)
    moved = await service.reschedule(domain.context, booked.id, move, "move")
    assert await service.reschedule(domain.context, booked.id, move, "move") == moved
    cancel = CancelAppointment(expected_version=2)
    cancelled = await service.cancel(domain.context, booked.id, cancel, "cancel")
    assert await service.cancel(domain.context, booked.id, cancel, "cancel") == cancelled
    assert await service.book(domain.context, domain.booking(), "book") == booked
    assert (await service.get(domain.context, booked.id)).status == "cancelled"
    assert [e.event_type for e in await outbox(tenancy, domain)] == [
        "appointment.created",
        "appointment.rescheduled",
        "appointment.cancelled",
    ]


async def test_event_failure_rolls_back_booking_audit_and_key(
    tenancy: TenancyFixture, domain: DomainFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("outbox insert failed")

    service = AppointmentService(domain.uow)
    with monkeypatch.context() as patch:
        patch.setattr(OutboxRepository, "appointment", fail)
        with pytest.raises(RuntimeError, match="outbox insert failed"):
            await service.book(domain.context, domain.booking(), "retry-after-rollback")
    assert await service.list(domain.context, 100, 0) == []
    assert await events(domain, AuditAction.APPOINTMENT_CREATED) == []
    assert await outbox(tenancy, domain) == []
    assert (
        await service.book(domain.context, domain.booking(), "retry-after-rollback")
    ).version == 1


async def test_keys_and_outbox_remain_tenant_isolated(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    other = await seed(domain.uow, tenancy.context(tenancy.b))
    service = AppointmentService(domain.uow)
    first = await service.book(domain.context, domain.booking(), "same-key")
    second = await service.book(other.context, other.booking(), "same-key")
    assert first.id != second.id
    for scope in (tenancy.a.tenant_id, tenancy.b.tenant_id, None):
        async with tenancy.database.session_factory.begin() as session:
            if scope:
                await set_tenant_scope(session, scope)
            for model in (IdempotencyRecord, OutboxEvent):
                rows = (await session.scalars(select(model))).all()
                assert len(rows) == (1 if scope else 0)
                assert all(r.tenant_id == scope for r in rows)


async def test_http_idempotency_header_replay_and_mismatch(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    headers = {
        "Authorization": f"Bearer {tenancy.a.token.get_secret_value()}",
        "Idempotency-Key": "http-book",
    }
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        payload = domain.booking().model_dump(mode="json")
        first = await client.post("/api/v1/appointments", json=payload)
        second = await client.post("/api/v1/appointments", json=payload)
        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()
        conflict = await client.post(
            "/api/v1/appointments", json=domain.booking(30).model_dump(mode="json")
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "idempotency_key_reused"
        )
        assert (
            await client.post(
                "/api/v1/appointments", json=payload, headers={"Idempotency-Key": "bad key"}
            )
        ).status_code == 422


async def test_provider_failure_preserves_commit_then_retries_safely(
    tenancy: TenancyFixture, domain: DomainFixture, worker: Database
) -> None:
    booked = await AppointmentService(domain.uow).book(domain.context, domain.booking(), "book")
    provider = RecordingProvider(failures=1)
    processor = EventProcessor(OutboxStore(worker.session_factory), provider)
    assert await processor.process_one()
    rows = await outbox(tenancy, domain)
    assert rows[0].status == "pending" and rows[0].attempts == 1
    assert rows[0].last_error == "provider_unavailable"
    assert (await AppointmentService(domain.uow).get(domain.context, booked.id)).status == "booked"
    await make_due(tenancy, domain)
    assert await processor.process_one()
    assert not await processor.process_one()
    row = (await outbox(tenancy, domain))[0]
    assert row.status == "delivered" and row.attempts == 2
    assert row.delivered_at is not None and row.lease_token is None
    assert provider.keys == [str(row.id), str(row.id)] and len(provider.accepted) == 1


async def test_multiple_workers_claim_once_and_preserve_aggregate_order(
    tenancy: TenancyFixture, domain: DomainFixture, worker: Database
) -> None:
    service = AppointmentService(domain.uow)
    booked = await service.book(domain.context, domain.booking())
    await service.cancel(domain.context, booked.id, CancelAppointment(expected_version=1))
    store = OutboxStore(worker.session_factory)
    results = await asyncio.gather(store.claim(), store.claim())
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1 and claimed[0].event_type == "appointment.created"
    assert await store.finish(claimed[0])
    next_event = await store.claim()
    assert next_event and next_event.event_type == "appointment.cancelled"
    assert await store.finish(next_event)


async def test_crash_after_delivery_redelivers_same_key_and_fences_old_worker(
    tenancy: TenancyFixture, domain: DomainFixture, worker: Database
) -> None:
    appointment = await AppointmentService(domain.uow).book(domain.context, domain.booking())
    store = OutboxStore(worker.session_factory)
    stale = await store.claim()
    assert stale
    provider = RecordingProvider()
    await provider.send(
        Notification(stale.id, stale.tenant_id, EventType.APPOINTMENT_CREATED, appointment),
        idempotency_key=str(stale.id),
    )
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == stale.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    current = await store.claim()
    assert current and current.lease_token != stale.lease_token
    assert not await store.finish(stale)
    await provider.send(
        Notification(current.id, current.tenant_id, EventType.APPOINTMENT_CREATED, appointment),
        idempotency_key=str(current.id),
    )
    assert await store.finish(current)
    assert len(provider.accepted) == 1 and len(provider.keys) == 2


async def test_retries_exhaust_to_dead_without_losing_appointment(
    tenancy: TenancyFixture, domain: DomainFixture, worker: Database
) -> None:
    booked = await AppointmentService(domain.uow).book(domain.context, domain.booking())
    processor = EventProcessor(
        OutboxStore(worker.session_factory),
        RecordingProvider(failures=10),
        retry=RetryPolicy(max_attempts=2),
    )
    assert await processor.process_one()
    await make_due(tenancy, domain)
    assert await processor.process_one()
    assert not await processor.process_one()
    assert (await outbox(tenancy, domain))[0].status == "dead"
    assert (await AppointmentService(domain.uow).get(domain.context, booked.id)).status == "booked"


@pytest.mark.parametrize(
    "mode,code,status",
    [
        ("permanent", "provider_rejected", "dead"),
        ("timeout", "provider_timeout", "pending"),
        ("unexpected", "provider_unavailable", "pending"),
    ],
)
async def test_typed_failures_timeouts_and_redaction(
    tenancy: TenancyFixture,
    domain: DomainFixture,
    worker: Database,
    mode: str,
    code: str,
    status: str,
) -> None:
    class FailingProvider:
        async def send(self, notification: Notification, *, idempotency_key: str) -> None:
            if mode == "permanent":
                raise PermanentProviderError("private provider payload")
            if mode == "timeout":
                await asyncio.sleep(1)
            else:
                raise RuntimeError("private provider payload")

    await AppointmentService(domain.uow).book(domain.context, domain.booking())
    assert await EventProcessor(
        OutboxStore(worker.session_factory), FailingProvider(), timeout_seconds=0.01
    ).process_one()
    row = (await outbox(tenancy, domain))[0]
    assert row.status == status and row.last_error == code


async def test_worker_and_api_database_privileges_are_separate(
    tenancy: TenancyFixture, domain: DomainFixture, worker: Database
) -> None:
    await AppointmentService(domain.uow).book(domain.context, domain.booking())
    with pytest.raises(UnsafeDatabaseRoleError):
        await OutboxStore(tenancy.database.session_factory).check()
    with pytest.raises(UnsafeDatabaseRoleError):
        await worker.ping()
    for model in (Patient, IdempotencyRecord, Appointment):
        with pytest.raises(DBAPIError):
            async with worker.session_factory.begin() as session:
                await session.scalars(select(model))
    with pytest.raises(DBAPIError):
        async with worker.session_factory.begin() as session:
            await session.execute(update(OutboxEvent).values(payload={}))
    with pytest.raises(DBAPIError):
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, domain.context.tenant_id)
            await session.execute(update(OutboxEvent).values(status="delivered"))
