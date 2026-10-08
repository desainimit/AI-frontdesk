import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.appointments.availability import AvailabilityService
from app.appointments.commands import (
    BookAppointment,
    CancelAppointment,
    DefineAvailability,
    RescheduleAppointment,
)
from app.appointments.errors import (
    AppointmentStateError,
    PastTimeError,
    ScheduleOverlapError,
    SlotUnavailableError,
    StaleAppointmentError,
)
from app.appointments.models import Appointment, Availability
from app.appointments.schemas import AppointmentView
from app.appointments.service import AppointmentService
from app.audit.events import AuditAction
from app.audit.models import AuditEvent
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.database.rls import set_tenant_scope
from app.database.session import Database
from app.database.unit_of_work import UnitOfWork
from app.errors import NotFoundError
from app.locations.models import Location
from app.locations.schemas import CreateLocation
from app.locations.service import LocationService
from app.main import create_app
from app.patients.models import Patient
from app.patients.schemas import CreatePatient
from app.patients.service import PatientService
from app.providers.models import Provider
from app.providers.schemas import CreateProvider
from app.providers.service import ProviderService
from app.users.models import User
from tests.fakes import FakeDependency
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


@dataclass
class DomainFixture:
    uow: UnitOfWork
    context: RequestContext
    location_id: UUID
    provider_id: UUID
    patient_id: UUID
    start: datetime

    def booking(self, minutes: int = 0) -> BookAppointment:
        return BookAppointment(
            provider_id=self.provider_id,
            patient_id=self.patient_id,
            starts_at=self.start + timedelta(minutes=minutes),
        )


async def seed(uow: UnitOfWork, context: RequestContext) -> DomainFixture:
    location = await LocationService(uow).create(context, CreateLocation(name="Clinic"))
    provider = await ProviderService(uow).create(
        context, CreateProvider(location_id=location.id, display_name="Provider")
    )
    patient = await PatientService(uow).create(
        context, CreatePatient(display_name="Synthetic Patient", phone="+919999999999")
    )
    start = (datetime.now(UTC) + timedelta(days=2)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    await AvailabilityService(uow).define(
        context,
        provider.id,
        DefineAvailability(starts_at=start, ends_at=start + timedelta(hours=2), slot_minutes=30),
    )
    return DomainFixture(uow, context, location.id, provider.id, patient.id, start)


@pytest.fixture
async def domain(tenancy: TenancyFixture) -> AsyncGenerator[DomainFixture]:
    # Multiple connections are essential: concurrency tests must not serialize in the pool.
    database = Database(
        tenancy.settings.model_copy(update={"db_pool_size": 4, "db_max_overflow": 0})
    )
    try:
        yield await seed(UnitOfWork(database.session_factory), tenancy.context(tenancy.a))
    finally:
        await database.close()


async def events(domain: DomainFixture, action: AuditAction) -> list[AuditEvent]:
    async with domain.uow.transaction(domain.context) as tx:
        return [e for e in await tx.audit.list(100, 0) if e.action == action]


async def test_http_booking_lifecycle_and_audit(tenancy: TenancyFixture) -> None:
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1/", headers=headers
        ) as client,
    ):
        response = await client.post(
            "locations", json={"name": "Clinic", "timezone": "Asia/Kolkata"}
        )
        assert response.status_code == 201
        response = await client.post(
            "providers", json={"location_id": response.json()["id"], "display_name": "Dr Test"}
        )
        assert response.status_code == 201
        provider = response.json()["id"]
        response = await client.post(
            "patients", json={"display_name": "Synthetic Patient", "phone": "+919999999999"}
        )
        assert response.status_code == 201
        patient = response.json()["id"]
        start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
        query = {
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=2)).isoformat(),
        }
        response = await client.post(
            f"providers/{provider}/availability", json={**query, "slot_minutes": 30}
        )
        assert response.status_code == 201
        assert (
            len((await client.get(f"providers/{provider}/availability", params=query)).json()) == 1
        )
        slots_url = f"providers/{provider}/slots"
        slots = (await client.get(slots_url, params=query)).json()
        assert len(slots) == 4
        booking = {
            "provider_id": provider,
            "patient_id": patient,
            "starts_at": slots[0]["starts_at"],
        }
        response = await client.post("appointments", json=booking)
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        appointment = response.json()
        identity = appointment["id"]
        assert appointment["version"] == 1
        assert (await client.post("appointments", json=booking)).status_code == 409
        assert len((await client.get(slots_url, params=query)).json()) == 3
        response = await client.post(
            f"appointments/{identity}/reschedule",
            json={"starts_at": slots[1]["starts_at"], "expected_version": 1},
        )
        assert response.status_code == 200
        assert response.json()["version"] == 2
        assert response.json()["starts_at"] == slots[1]["starts_at"]
        assert (
            await client.post(f"appointments/{identity}/cancel", json={"expected_version": 1})
        ).status_code == 409
        for _ in range(2):
            response = await client.post(
                f"appointments/{identity}/cancel", json={"expected_version": 2}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "cancelled"
            assert response.json()["version"] == 3
        assert len((await client.get(slots_url, params=query)).json()) == 4
        assert (await client.get(f"appointments/{identity}")).status_code == 200
        assert len((await client.get("appointments")).json()) == 1
        assert (await client.get(f"patients/{patient}")).status_code == 200
        assert len((await client.get("patients")).json()) == 1
        audit = (await client.get("audit-events")).json()
        for action in (
            "location_created",
            "provider_created",
            "patient_created",
            "availability_defined",
            "appointment_created",
            "appointment_rescheduled",
            "appointment_cancelled",
        ):
            assert sum(e["action"] == action for e in audit) == 1
        assert "Synthetic Patient" not in str(audit)
        assert "+919999999999" not in str(audit)


async def test_http_validation_permissions_and_cross_tenant(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    other = await seed(domain.uow, tenancy.context(tenancy.b))
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1/", headers=headers
        ) as client,
    ):
        assert (await client.get(f"patients/{other.patient_id}")).status_code == 404
        assert (
            await client.post(
                "providers", json={"display_name": "Test", "location_id": str(other.location_id)}
            )
        ).status_code == 404
        assert (
            await client.post("appointments", json=other.booking().model_dump(mode="json"))
        ).status_code == 404
        assert (
            await client.post(
                "patients",
                json={
                    "display_name": "Test",
                    "phone": "+919999999999",
                    "tenant_id": str(tenancy.b.tenant_id),
                },
            )
        ).status_code == 422
        assert (
            await client.post(
                "appointments",
                json={
                    **domain.booking().model_dump(mode="json"),
                    "starts_at": "2030-01-01T09:00:00",
                },
            )
        ).status_code == 422
        query = {
            "starts_at": domain.start.isoformat(),
            "ends_at": (domain.start + timedelta(days=32)).isoformat(),
        }
        assert (
            await client.get(f"providers/{domain.provider_id}/slots", params=query)
        ).status_code == 422
        async with tenancy.admin.begin() as session:
            await session.execute(
                update(User)
                .where(User.id == tenancy.a.user_id)
                .values(role="member", role_id=uuid5(tenancy.a.tenant_id, "role:member"))
            )
        assert (await client.get("providers")).status_code == 200
        assert (await client.get("patients")).status_code == 403
        assert (await client.get("appointments")).status_code == 403
        assert (
            await client.post("appointments", json=domain.booking().model_dump(mode="json"))
        ).status_code == 403
        assert (await client.post("locations", json={"name": "Blocked"})).status_code == 403


async def test_concurrent_booking_has_one_winner_and_one_audit(domain: DomainFixture) -> None:
    service = AppointmentService(domain.uow)
    results = await asyncio.gather(
        *(service.book(domain.context, domain.booking()) for _ in range(2)), return_exceptions=True
    )
    assert sum(isinstance(result, AppointmentView) for result in results) == 1
    assert sum(isinstance(result, SlotUnavailableError) for result in results) == 1
    assert len(await service.list(domain.context, 100, 0)) == 1
    assert len(await events(domain, AuditAction.APPOINTMENT_CREATED)) == 1


async def test_failed_reschedule_is_atomic_and_versioned(domain: DomainFixture) -> None:
    service = AppointmentService(domain.uow)
    first = await service.book(domain.context, domain.booking())
    second = await service.book(domain.context, domain.booking(30))
    with pytest.raises(SlotUnavailableError):
        await service.reschedule(
            domain.context,
            first.id,
            RescheduleAppointment(starts_at=second.starts_at, expected_version=1),
        )
    unchanged = await service.get(domain.context, first.id)
    assert unchanged.starts_at == first.starts_at and unchanged.version == 1
    assert await events(domain, AuditAction.APPOINTMENT_RESCHEDULED) == []
    moved = await service.reschedule(
        domain.context,
        first.id,
        RescheduleAppointment(starts_at=domain.start + timedelta(hours=1), expected_version=1),
    )
    assert moved.version == 2
    with pytest.raises(StaleAppointmentError):
        await service.cancel(domain.context, first.id, CancelAppointment(expected_version=1))
    await service.cancel(domain.context, first.id, CancelAppointment(expected_version=2))
    with pytest.raises(AppointmentStateError):
        await service.reschedule(
            domain.context,
            first.id,
            RescheduleAppointment(starts_at=domain.start, expected_version=3),
        )
    # Cancellation releases the provider and patient slot.
    assert (await service.book(domain.context, domain.booking(60))).status == "booked"


async def test_schedule_and_booking_validation(domain: DomainFixture) -> None:
    service = AppointmentService(domain.uow)
    for minutes in (-30, 1, 120):
        with pytest.raises(SlotUnavailableError):
            await service.book(domain.context, domain.booking(minutes))
    with pytest.raises(PastTimeError):
        await service.book(
            domain.context,
            domain.booking().model_copy(
                update={"starts_at": datetime.now(UTC) - timedelta(days=1)}
            ),
        )
    with pytest.raises(ScheduleOverlapError):
        await AvailabilityService(domain.uow).define(
            domain.context,
            domain.provider_id,
            DefineAvailability(
                starts_at=domain.start + timedelta(minutes=30),
                ends_at=domain.start + timedelta(hours=3),
                slot_minutes=30,
            ),
        )


async def test_failed_audit_rolls_back_booking(
    domain: DomainFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit storage failed")

    service = AppointmentService(domain.uow)
    with monkeypatch.context() as patch:
        patch.setattr(AuditService, "record", fail)
        with pytest.raises(RuntimeError, match="audit storage failed"):
            await service.book(domain.context, domain.booking())
    assert await service.list(domain.context, 100, 0) == []
    assert await events(domain, AuditAction.APPOINTMENT_CREATED) == []


async def test_domain_rls_and_composite_foreign_keys(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    other = await seed(domain.uow, tenancy.context(tenancy.b))
    await AppointmentService(domain.uow).book(domain.context, domain.booking())
    await AppointmentService(domain.uow).book(other.context, other.booking())
    models = (Location, Provider, Patient, Availability, Appointment)
    for scope in (tenancy.a.tenant_id, tenancy.b.tenant_id, None):
        async with tenancy.database.session_factory.begin() as session:
            if scope is not None:
                await set_tenant_scope(session, scope)
            for model in models:
                rows = (await session.scalars(select(model))).all()  # deliberately no WHERE
                assert len(rows) == (0 if scope is None else 1)
                assert all(row.tenant_id == scope for row in rows)
    with pytest.raises(DBAPIError):
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, tenancy.a.tenant_id)
            session.add(
                Patient(
                    tenant_id=tenancy.b.tenant_id, display_name="Blocked", phone="+919999999999"
                )
            )
    with pytest.raises(IntegrityError):
        async with tenancy.admin.begin() as session:
            session.add(
                Provider(
                    tenant_id=tenancy.a.tenant_id,
                    location_id=other.location_id,
                    display_name="Blocked",
                )
            )
    with pytest.raises(IntegrityError):
        async with tenancy.admin.begin() as session:
            session.add(
                Appointment(
                    tenant_id=tenancy.a.tenant_id,
                    provider_id=domain.provider_id,
                    patient_id=other.patient_id,
                    starts_at=domain.start + timedelta(hours=1),
                    ends_at=domain.start + timedelta(minutes=90),
                )
            )
    with pytest.raises(NotFoundError):
        await AppointmentService(domain.uow).book(domain.context, other.booking())


async def test_database_exclusions_block_partial_overlap_and_patient_conflict(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    await AppointmentService(domain.uow).book(domain.context, domain.booking())
    other_provider = await ProviderService(domain.uow).create(
        domain.context,
        CreateProvider(location_id=domain.location_id, display_name="Second Provider"),
    )
    for provider in (domain.provider_id, other_provider.id):
        with pytest.raises(IntegrityError) as error:
            async with tenancy.database.session_factory.begin() as session:
                await set_tenant_scope(session, domain.context.tenant_id)
                session.add(
                    Appointment(
                        tenant_id=domain.context.tenant_id,
                        provider_id=provider,
                        patient_id=domain.patient_id,
                        starts_at=domain.start + timedelta(minutes=15),
                        ends_at=domain.start + timedelta(minutes=45),
                    )
                )
        assert getattr(error.value.orig, "sqlstate", None) == "23P01"


async def test_past_appointment_cannot_be_cancelled_or_rescheduled(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    service = AppointmentService(domain.uow)
    booked = await service.book(domain.context, domain.booking())
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == booked.id)
            .values(
                starts_at=domain.start - timedelta(days=5),
                ends_at=domain.start - timedelta(days=5) + timedelta(minutes=30),
            )
        )
    with pytest.raises(AppointmentStateError):
        await service.cancel(domain.context, booked.id, CancelAppointment(expected_version=1))
    with pytest.raises(AppointmentStateError):
        await service.reschedule(
            domain.context,
            booked.id,
            RescheduleAppointment(starts_at=domain.start, expected_version=1),
        )
