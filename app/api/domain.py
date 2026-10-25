from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response

from app.api.tenancy import (
    Context,
    Cursor,
    DatabaseDependency,
    Limit,
    Search,
    Since,
    Until,
)
from app.appointments.availability import AvailabilityService
from app.appointments.commands import (
    BookAppointment,
    CancelAppointment,
    RescheduleAppointment,
    SlotRequest,
)
from app.appointments.schemas import AppointmentView, Slot
from app.appointments.service import AppointmentService
from app.calendar_sync.service import CalendarService
from app.clinic.schemas import (
    ProviderScheduleView,
    ServiceCommand,
    ServiceView,
    TimeOffCommand,
    TimeOffView,
    WeeklySchedule,
)
from app.clinic.service import ClinicService
from app.core.database.pages import Page, decode_cursor, page
from app.core.database.unit_of_work import UnitOfWork
from app.locations.schemas import CreateLocation, LocationView
from app.locations.service import LocationService
from app.patients.schemas import (
    CreatePatient,
    PatientView,
    UpdatePatient,
    UpdatePatientVerification,
)
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider, ProviderView, UpdateProvider
from app.providers.service import ProviderService

router = APIRouter(prefix="/api/v1", tags=["domain"])
IdempotencyKey = Annotated[
    str | None,
    Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
]


def _appointments(request: Request, database: DatabaseDependency) -> AppointmentService:
    """Bookings that ask the provider's own calendar once more before saving."""
    return AppointmentService(
        UnitOfWork(database.session_factory),
        CalendarService(
            database.session_factory,
            request.app.state.settings,
            getattr(request.app.state, "provider_transport", None),
        ),
    )


@router.post("/locations", status_code=201)
async def create_location(
    command: CreateLocation, context: Context, database: DatabaseDependency
) -> LocationView:
    return await LocationService(UnitOfWork(database.session_factory)).create(context, command)


@router.put("/locations/{location_id}")
async def update_location(
    location_id: UUID, command: CreateLocation, context: Context, database: DatabaseDependency
) -> LocationView:
    return await LocationService(UnitOfWork(database.session_factory)).update(
        context, location_id, command
    )


@router.get("/locations")
async def list_locations(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    q: Search = None,
) -> Page[LocationView]:
    offset = decode_cursor(cursor)
    rows = await LocationService(UnitOfWork(database.session_factory)).list(
        context, limit + 1, offset, q
    )
    return page(rows, limit, offset)


@router.post("/providers", status_code=201)
async def create_provider(
    command: CreateProvider, context: Context, database: DatabaseDependency
) -> ProviderView:
    return await ProviderService(UnitOfWork(database.session_factory)).create(context, command)


@router.put("/providers/{provider_id}")
async def update_provider(
    provider_id: UUID, command: UpdateProvider, context: Context, database: DatabaseDependency
) -> ProviderView:
    return await ProviderService(UnitOfWork(database.session_factory)).update(
        context, provider_id, command
    )


@router.get("/providers")
async def list_providers(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    q: Search = None,
    location_id: UUID | None = None,
    is_active: bool | None = None,
) -> Page[ProviderView]:
    offset = decode_cursor(cursor)
    rows = await ProviderService(UnitOfWork(database.session_factory)).list(
        context, limit + 1, offset, q, location_id, is_active
    )
    return page(rows, limit, offset)


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
    cursor: Cursor = None,
    q: Search = None,
) -> Page[PatientView]:
    offset = decode_cursor(cursor)
    rows = await PatientService(UnitOfWork(database.session_factory)).list(
        context, limit + 1, offset, q
    )
    return page(rows, limit, offset)


@router.get("/patients/{patient_id}")
async def get_patient(
    patient_id: UUID, context: Context, database: DatabaseDependency
) -> PatientView:
    return await PatientService(UnitOfWork(database.session_factory)).get(context, patient_id)


@router.put("/patients/{patient_id}")
async def update_patient(
    patient_id: UUID, command: UpdatePatient, context: Context, database: DatabaseDependency
) -> PatientView:
    return await PatientService(UnitOfWork(database.session_factory)).update(
        context, patient_id, command
    )


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


@router.get("/providers/{provider_id}/slots")
async def list_slots(
    provider_id: UUID,
    query: Annotated[SlotRequest, Query()],
    context: Context,
    database: DatabaseDependency,
) -> list[Slot]:
    """Free times for one provider. The appointment type sets how long each slot is."""
    return await AvailabilityService(UnitOfWork(database.session_factory)).slots(
        context, provider_id, query, query.service_id
    )


@router.get("/providers/{provider_id}/schedule")
async def provider_schedule(
    provider_id: UUID, context: Context, database: DatabaseDependency
) -> ProviderScheduleView:
    return await ClinicService(UnitOfWork(database.session_factory)).schedule(context, provider_id)


@router.put("/providers/{provider_id}/schedule")
async def set_provider_schedule(
    provider_id: UUID, command: WeeklySchedule, context: Context, database: DatabaseDependency
) -> ProviderScheduleView:
    """Replace the provider's working week."""
    return await ClinicService(UnitOfWork(database.session_factory)).set_schedule(
        context, provider_id, command
    )


@router.post("/providers/{provider_id}/time-off", status_code=201)
async def add_time_off(
    provider_id: UUID, command: TimeOffCommand, context: Context, database: DatabaseDependency
) -> TimeOffView:
    return await ClinicService(UnitOfWork(database.session_factory)).add_time_off(
        context, provider_id, command
    )


@router.delete("/providers/{provider_id}/time-off/{time_off_id}", status_code=204)
async def remove_time_off(
    provider_id: UUID, time_off_id: UUID, context: Context, database: DatabaseDependency
) -> Response:
    await ClinicService(UnitOfWork(database.session_factory)).remove_time_off(
        context, provider_id, time_off_id
    )
    return Response(status_code=204)


@router.get("/services")
async def list_services(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    active_only: bool = False,
) -> Page[ServiceView]:
    offset = decode_cursor(cursor)
    rows = await ClinicService(UnitOfWork(database.session_factory)).list_services(
        context, limit + 1, offset, active_only=active_only
    )
    return page(rows, limit, offset)


@router.post("/services", status_code=201)
async def create_service(
    command: ServiceCommand, context: Context, database: DatabaseDependency
) -> ServiceView:
    return await ClinicService(UnitOfWork(database.session_factory)).save_service(context, command)


@router.put("/services/{service_id}")
async def update_service(
    service_id: UUID, command: ServiceCommand, context: Context, database: DatabaseDependency
) -> ServiceView:
    return await ClinicService(UnitOfWork(database.session_factory)).save_service(
        context, command, service_id
    )


@router.post("/appointments", status_code=201)
async def book_appointment(
    command: BookAppointment,
    request: Request,
    context: Context,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey = None,
) -> AppointmentView:
    return await _appointments(request, database).book(context, command, idempotency_key)


@router.get("/appointments")
async def list_appointments(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Literal["booked", "cancelled"] | None = None,
    provider_id: UUID | None = None,
    patient_id: UUID | None = None,
    starts_from: Since = None,
    starts_to: Until = None,
    order: Literal["asc", "desc"] = "asc",
    # The number a patient quotes: "A-1042" or just "1042".
    number: Annotated[
        str | None, Query(max_length=12, pattern=r"^\s*(?:[Aa]-?)?\d{1,9}\s*$")
    ] = None,
) -> Page[AppointmentView]:
    offset = decode_cursor(cursor)
    rows = await AppointmentService(UnitOfWork(database.session_factory)).list(
        context,
        limit + 1,
        offset,
        status=status,
        provider_id=provider_id,
        patient_id=patient_id,
        starts_from=starts_from,
        starts_to=starts_to,
        newest_first=order == "desc",
        number=int("".join(c for c in number if c.isdigit())) if number else None,
    )
    return page(rows, limit, offset)


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
    request: Request,
    context: Context,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey = None,
) -> AppointmentView:
    return await _appointments(request, database).reschedule(
        context, appointment_id, command, idempotency_key
    )
