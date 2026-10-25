"""Every appointment gets a number patients and staff can quote, and texts carry it."""

from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.appointments.commands import BookAppointment, RescheduleAppointment
from app.appointments.service import AppointmentService
from app.core.database.unit_of_work import UnitOfWork
from app.notifications.models import OutboundMessage
from app.settings.schemas import SettingsSection
from app.settings.service import SettingsService
from tests.evals.harness import EvalClinic
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


def at(days: int, hour: int) -> datetime:
    return (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


async def test_appointments_are_numbered_per_clinic_and_texts_carry_the_number(
    tenancy: TenancyFixture,
) -> None:
    clinic = await EvalClinic(tenancy).open()
    # The clinic's own wording, written before numbers existed, has no placeholder.
    settings = SettingsService(clinic.sessions)
    notifications = (await settings.get(clinic.owner)).settings.notifications.model_dump(
        mode="json"
    )
    notifications["confirmation_text"] = "See you {{starts_at}} at {{clinic_name}}."
    await settings.save(clinic.owner, SettingsSection.NOTIFICATIONS, notifications)

    service = AppointmentService(UnitOfWork(clinic.sessions))
    first = await service.book(
        clinic.owner,
        BookAppointment(
            provider_id=clinic.provider_id, patient_id=clinic.patient_id, starts_at=at(3, 10)
        ),
        f"numbers-{uuid4().hex}",
    )
    second = await service.book(
        clinic.owner,
        BookAppointment(
            provider_id=clinic.provider_id, patient_id=clinic.patient_id, starts_at=at(4, 11)
        ),
        f"numbers-{uuid4().hex}",
    )
    assert (first.number, first.reference) == (1, "A-1")
    assert (second.number, second.reference) == (2, "A-2")
    # Moving it keeps its number: it is the same appointment.
    moved = await service.reschedule(
        clinic.owner,
        first.id,
        RescheduleAppointment(starts_at=at(5, 12), expected_version=first.version),
        f"numbers-{uuid4().hex}",
    )
    assert moved.reference == "A-1"
    # Staff lists show the number too.
    listed = await service.list(clinic.owner, 10, 0)
    assert sorted(row.reference for row in listed) == ["A-1", "A-2"]
    # A patient quoting their number is found at once.
    found = await service.list(clinic.owner, 10, 0, number=2)
    assert [row.id for row in found] == [second.id]

    bodies = [row.body for row in await clinic.rows(OutboundMessage) if row.channel == "sms"]
    assert any("A-1" in body for body in bodies) and any("A-2" in body for body in bodies)
    assert all("Appointment number: A-" in body for body in bodies if "See you" in body)
