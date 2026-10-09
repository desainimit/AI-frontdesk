from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query

from app.api.tenancy import (
    Context,
    DatabaseDependency,
    Limit,
    Offset,
    Search,
    Since,
    Until,
)
from app.appointments.availability import AvailabilityService
from app.appointments.commands import (
    BookAppointment,
    CancelAppointment,
    DefineAvailability,
    RescheduleAppointment,
    SlotQuery,
)
from app.appointments.schemas import AppointmentView, AvailabilityView, Slot
from app.appointments.service import AppointmentService
from app.core.database.unit_of_work import UnitOfWork
from app.locations.schemas import CreateLocation, LocationView
from app.locations.service import LocationService
from app.patients.schemas import CreatePatient, PatientView, UpdatePatientVerification
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider, ProviderView
from app.providers.service import ProviderService

router = APIRouter(prefix="/api/v1", tags=["domain"])
WindowQuery = Annotated[SlotQuery, Query()]
IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


@router.post("/locations", status_code=201)
async def create_location(
    command: CreateLocation, context: Context, database: DatabaseDependency
) -> LocationView:
    return await LocationService(UnitOfWork(database.session_factory)).create(context, command)


@router.get("/locations")
async def list_locations(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    q: Search = None,
) -> list[LocationView]:
    return await LocationService(UnitOfWork(database.session_factory)).list(
        context, limit, offset, q
    )


@router.post("/providers", status_code=201)
async def create_provider(
    command: CreateProvider, context: Context, database: DatabaseDependency
) -> ProviderView:
    return await ProviderService(UnitOfWork(database.session_factory)).create(context, command)


@router.get("/providers")
async def list_providers(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    q: Search = None,
    location_id: UUID | None = None,
) -> list[ProviderView]:
    return await ProviderService(UnitOfWork(database.session_factory)).list(
        context, limit, offset, q, location_id
    )


@router.post("/patients", status_code=201)
async def create_patient(
    command: CreatePatient, context: Context, database: DatabaseDependency
) -> PatientView:
    return await PatientService(UnitOfWork(database.session_factory)).create(context, command)


@router.get("/patients")
async def list_patients(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    q: Search = None,
) -> list[PatientView]:
    return await PatientService(UnitOfWork(database.session_factory)).list(
        context, limit, offset, q
    )


@router.get("/patients/{patient_id}")
async def get_patient(
    patient_id: UUID, context: Context, database: DatabaseDependency
) -> PatientView:
    return await PatientService(UnitOfWork(database.session_factory)).get(context, patient_id)


@router.put("/patients/{patient_id}/verification")
async def update_patient_verification(
    patient_id: UUID,
    command: UpdatePatientVerification,
    context: Context,
    database: DatabaseDependency,
) -> PatientView:
    return await PatientService(UnitOfWork(database.session_factory)).set_verification(
        context, patient_id, command
    )


@router.post("/providers/{provider_id}/availability", status_code=201)
async def define_availability(
    provider_id: UUID, command: DefineAvailability, context: Context, database: DatabaseDependency
) -> AvailabilityView:
    return await AvailabilityService(UnitOfWork(database.session_factory)).define(
        context, provider_id, command
    )


@router.get("/providers/{provider_id}/availability")
async def list_availability(
    provider_id: UUID, query: WindowQuery, context: Context, database: DatabaseDependency
) -> list[AvailabilityView]:
    return await AvailabilityService(UnitOfWork(database.session_factory)).list_windows(
        context, provider_id, query
    )


@router.get("/providers/{provider_id}/slots")
async def list_slots(
    provider_id: UUID, query: WindowQuery, context: Context, database: DatabaseDependency
) -> list[Slot]:
    return await AvailabilityService(UnitOfWork(database.session_factory)).slots(
        context, provider_id, query
    )


@router.post("/appointments", status_code=201)
async def book_appointment(
    command: BookAppointment,
    context: Context,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey = None,
) -> AppointmentView:
    return await AppointmentService(UnitOfWork(database.session_factory)).book(
        context, command, idempotency_key
    )


@router.get("/appointments")
async def list_appointments(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    status: Literal["booked", "cancelled"] | None = None,
    provider_id: UUID | None = None,
    patient_id: UUID | None = None,
    starts_from: Since = None,
    starts_to: Until = None,
    order: Literal["asc", "desc"] = "asc",
) -> list[AppointmentView]:
    return await AppointmentService(UnitOfWork(database.session_factory)).list(
        context,
        limit,
        offset,
        status=status,
        provider_id=provider_id,
        patient_id=patient_id,
        starts_from=starts_from,
        starts_to=starts_to,
        newest_first=order == "desc",
    )


@router.get("/appointments/{appointment_id}")
async def get_appointment(
    appointment_id: UUID, context: Context, database: DatabaseDependency
) -> AppointmentView:
    return await AppointmentService(UnitOfWork(database.session_factory)).get(
        context, appointment_id
    )


@router.post("/appointments/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: UUID,
    command: CancelAppointment,
    context: Context,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey = None,
) -> AppointmentView:
    return await AppointmentService(UnitOfWork(database.session_factory)).cancel(
        context, appointment_id, command, idempotency_key
    )


@router.post("/appointments/{appointment_id}/reschedule")
async def reschedule_appointment(
    appointment_id: UUID,
    command: RescheduleAppointment,
    context: Context,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey = None,
) -> AppointmentView:
    return await AppointmentService(UnitOfWork(database.session_factory)).reschedule(
        context, appointment_id, command, idempotency_key
    )
