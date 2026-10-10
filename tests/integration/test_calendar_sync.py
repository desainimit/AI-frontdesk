"""A provider's own calendar: connect, write bookings to it, respect what is busy there."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.appointments.availability import AvailabilityService
from app.appointments.commands import BookAppointment, CancelAppointment, SlotQuery
from app.appointments.errors import SlotUnavailableError
from app.appointments.service import AppointmentService
from app.calendar_sync.models import AppointmentCalendarLink, CalendarConnection
from app.calendar_sync.service import (
    CalendarService,
    CalendarSignInFailedError,
    CompleteCalendar,
    ConnectCalendar,
)
from app.calendar_sync.sync import CalendarSync
from app.clinic.schemas import ScheduleWindow, WeeklySchedule
from app.clinic.service import ClinicService
from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.core.database.unit_of_work import UnitOfWork
from app.events.processor import EventProcessor
from app.events.providers import RetryPolicy
from app.events.store import OutboxStore
from app.locations.schemas import CreateLocation
from app.locations.service import LocationService
from app.patients.schemas import CreatePatient
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider
from app.providers.service import ProviderService
from tests.integration.identity_harness import mail_settings
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com"
EVENTS = "/calendar/v3/calendars/primary/events"


class FakeGoogle:
    """Stands in for Google's sign-in and calendar; nothing leaves this process."""

    def __init__(self) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.revoked = False
        self.calls: list[tuple[str, str]] = []

    def add(self, event_id: str, starts_at: datetime, ends_at: datetime) -> None:
        self.events[event_id] = {
            "id": event_id,
            "status": "confirmed",
            "start": {"dateTime": starts_at.astimezone(UTC).isoformat()},
            "end": {"dateTime": ends_at.astimezone(UTC).isoformat()},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.url.host == "oauth2.googleapis.com":
            if self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            form = parse_qs(request.content.decode())
            assert form["client_id"] == [CLIENT_ID] and form["client_secret"] == ["s" * 24]
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt"})
        assert request.headers["authorization"] == "Bearer at"
        if path.endswith("/userinfo"):
            return httpx.Response(200, json={"email": "dr.rao@example.test"})
        if request.method == "GET" and path == EVENTS:
            return httpx.Response(200, json={"items": list(self.events.values())})
        if request.method == "POST" and path == EVENTS:
            body = json.loads(request.content)
            self.events[body["id"]] = {**body, "status": "confirmed"}
            return httpx.Response(200, json={"id": body["id"]})
        event_id = path.rsplit("/", 1)[-1]
        if event_id not in self.events:
            return httpx.Response(404, json={})
        if request.method == "DELETE":
            del self.events[event_id]
            return httpx.Response(204)
        self.events[event_id].update(json.loads(request.content))
        return httpx.Response(200, json={"id": event_id})


async def test_bookings_reach_the_calendar_and_its_busy_time_blocks_slots(
    tenancy: TenancyFixture,
) -> None:
    google = FakeGoogle()
    transport = httpx.MockTransport(google)
    settings = mail_settings(tenancy.settings).model_copy(
        update={
            "google_calendar_client_id": CLIENT_ID,
            "google_calendar_client_secret": SecretStr("s" * 24),
        }
    )
    sessions = tenancy.database.session_factory
    uow = UnitOfWork(sessions)
    owner = tenancy.context(tenancy.a)
    tenant_id = tenancy.a.tenant_id
    location = await LocationService(uow).create(owner, CreateLocation(name="Main clinic"))
    provider = await ProviderService(uow).create(
        owner, CreateProvider(location_id=location.id, display_name="Dr. Rao")
    )
    await ClinicService(uow).set_schedule(
        owner,
        provider.id,
        WeeklySchedule(
            windows=[ScheduleWindow(weekday=day, starts="08:00", ends="20:00") for day in range(7)]  # pyright: ignore[reportArgumentType]
        ),
    )
    patient = await PatientService(uow).create(
        owner, CreatePatient(display_name="Meera Shah", phone="+919812345678")
    )

    # ---- connect ---------------------------------------------------------------------
    calendars = CalendarService(sessions, settings, transport)
    assert (await calendars.options(owner)).google is True
    started = await calendars.start(owner, provider.id, ConnectCalendar(kind="google"))
    query = parse_qs(urlsplit(started.url).query)
    assert urlsplit(started.url).netloc == "accounts.google.com"
    assert query["client_id"] == [CLIENT_ID] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["https://app.example.test/api/calendar/callback"]
    state = query["state"][0]
    with pytest.raises(CalendarSignInFailedError):
        await calendars.complete(owner, CompleteCalendar(code="code", state="x" * 43))
    # Another clinic cannot finish a sign-in this one started.
    with pytest.raises(CalendarSignInFailedError):
        await calendars.complete(
            tenancy.context(tenancy.b), CompleteCalendar(code="code", state=state)
        )
    status = await calendars.complete(owner, CompleteCalendar(code="code", state=state))
    assert status.connected and status.account_email == "dr.rao@example.test"
    with pytest.raises(CalendarSignInFailedError):  # A state works once.
        await calendars.complete(owner, CompleteCalendar(code="code", state=state))
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenant_id)
        stored = await session.scalar(select(CalendarConnection))
        assert stored is not None and b"rt" not in stored.ciphertext

    # ---- a booking is written to the calendar ----------------------------------------
    zone = ZoneInfo("Asia/Kolkata")
    start = (datetime.now(zone) + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    appointments = AppointmentService(uow, calendars)
    booked = await appointments.book(
        owner, BookAppointment(provider_id=provider.id, patient_id=patient.id, starts_at=start)
    )
    assert isinstance(tenancy.settings.worker_database_url, SecretStr)
    worker_settings = settings.model_copy(
        update={"database_url": tenancy.settings.worker_database_url}
    )
    worker = Database(worker_settings)
    sync = CalendarSync(worker.session_factory, worker_settings, transport)
    outbox = EventProcessor(
        OutboxStore(worker.session_factory, 90), sync, retry=RetryPolicy(max_attempts=1)
    )

    async def link() -> AppointmentCalendarLink | None:
        async with tenancy.admin.begin() as session:
            await set_tenant_scope(session, tenant_id)
            return await session.scalar(
                select(AppointmentCalendarLink).where(
                    AppointmentCalendarLink.appointment_id == booked.id
                )
            )

    async def due() -> None:
        async with tenancy.admin.begin() as session:
            await session.execute(
                update(CalendarConnection)
                .where(CalendarConnection.tenant_id == tenant_id)
                .values(last_synced_at=None)
            )

    try:
        while await outbox.process_one():
            pass
        written = await link()
        assert written is not None and written.status == "synced"
        event = google.events[written.external_event_id]
        # No patient name in the provider's calendar unless the clinic asks for it.
        assert event["summary"] == "Appointment" and "Meera" not in str(event)
        assert event["start"]["dateTime"] == start.astimezone(UTC).isoformat()

        # ---- busy time in the calendar removes slots ---------------------------------
        lunch = start + timedelta(hours=2)
        google.add("lunch", lunch, lunch + timedelta(hours=1))
        await due()
        assert await sync.pull_one() is True
        day = SlotQuery(starts_at=start.replace(hour=8), ends_at=start.replace(hour=20))
        free = {
            slot.starts_at for slot in await AvailabilityService(uow).slots(owner, provider.id, day)
        }
        assert (
            lunch.astimezone(UTC) not in free
            and (lunch + timedelta(hours=1)).astimezone(UTC) in free
        )
        assert start.astimezone(UTC) not in free  # the booking itself
        with pytest.raises(SlotUnavailableError):
            await appointments.book(
                owner,
                BookAppointment(provider_id=provider.id, patient_id=patient.id, starts_at=lunch),
            )
        # Something added a moment ago, before any sync, is caught by asking the calendar.
        evening = start + timedelta(hours=6)
        google.add("dinner", evening, evening + timedelta(minutes=30))
        with pytest.raises(SlotUnavailableError):
            await appointments.book(
                owner,
                BookAppointment(provider_id=provider.id, patient_id=patient.id, starts_at=evening),
            )

        # ---- the provider moves our event: flagged, not changed -----------------------
        google.events[written.external_event_id]["start"] = {
            "dateTime": (start + timedelta(hours=1)).astimezone(UTC).isoformat()
        }
        google.events[written.external_event_id]["end"] = {
            "dateTime": (start + timedelta(hours=1, minutes=30)).astimezone(UTC).isoformat()
        }
        await due()
        await sync.pull_one()
        flagged = await link()
        assert flagged is not None and flagged.status == "conflict"
        options = await calendars.options(owner)
        assert options.connections[0].conflicts == 1
        unchanged = await appointments.get(owner, booked.id)
        assert unchanged.starts_at == start.astimezone(UTC)

        # ---- a cancellation removes the event ------------------------------------------
        await appointments.cancel(owner, booked.id, CancelAppointment(expected_version=1))
        while await outbox.process_one():
            pass
        assert written.external_event_id not in google.events and await link() is None

        # ---- consent withdrawn: the connection asks for attention ----------------------
        google.revoked = True
        await due()
        await sync.pull_one()
        lost = (await calendars.options(owner)).connections[0]
        assert lost.status == "needs_attention" and "Connect it again" in (lost.problem or "")
        # A booking is never refused because the calendar could not be asked.
        again = await appointments.book(
            owner,
            BookAppointment(
                provider_id=provider.id, patient_id=patient.id, starts_at=start + timedelta(days=1)
            ),
        )
        assert again.status == "booked"
        await calendars.disconnect(owner, UUID(str(provider.id)))
        assert (await calendars.options(owner)).connections == []
    finally:
        await worker.close()
