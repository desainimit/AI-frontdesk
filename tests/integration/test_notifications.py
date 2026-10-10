"""Patient messages: queued with the booking, sent by the worker, visible in a log."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.appointments.commands import BookAppointment, CancelAppointment, RescheduleAppointment
from app.appointments.service import AppointmentService
from app.calls.models import PhoneNumber
from app.clinic.schemas import ScheduleWindow, WeeklySchedule
from app.clinic.service import ClinicService
from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.core.database.unit_of_work import UnitOfWork
from app.events.providers import RetryPolicy
from app.events.store import verify_worker_role
from app.integrations.resolver import TenantIntegrationResolver
from app.locations.schemas import CreateLocation
from app.locations.service import LocationService
from app.notifications import delivery
from app.notifications.delivery import MessageDelivery
from app.notifications.models import OutboundMessage
from app.notifications.service import NoClinicNumberError, NotificationService, TestMessage
from app.patients.schemas import CreatePatient, UpdatePatient
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider
from app.providers.service import ProviderService
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

ACCOUNT = "AC" + "9" * 32
CLINIC_NUMBER = "+14155550100"


async def messages(tenancy: TenancyFixture, tenant_id: object) -> list[OutboundMessage]:
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenant_id)  # pyright: ignore[reportArgumentType]
        rows = await session.scalars(
            select(OutboundMessage)
            .where(OutboundMessage.tenant_id == tenant_id)
            .order_by(OutboundMessage.created_at, OutboundMessage.kind, OutboundMessage.channel)
        )
        return list(rows)


async def test_a_booking_queues_its_messages_and_the_worker_sends_them(
    tenancy: TenancyFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    uow = UnitOfWork(tenancy.database.session_factory)
    owner = tenancy.context(tenancy.a)
    tenant_id = tenancy.a.tenant_id
    clinic_number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenant_id)
        session.add(PhoneNumber(tenant_id=tenant_id, number=clinic_number))
    location = await LocationService(uow).create(owner, CreateLocation(name="Main clinic"))
    provider = await ProviderService(uow).create(
        owner, CreateProvider(location_id=location.id, display_name="Dr. Rao")
    )
    # Works every day, so "three days from now" is always bookable.
    await ClinicService(uow).set_schedule(
        owner,
        provider.id,
        WeeklySchedule(
            windows=[ScheduleWindow(weekday=day, starts="08:00", ends="20:00") for day in range(7)]  # pyright: ignore[reportArgumentType]
        ),
    )
    patient = await PatientService(uow).create(
        owner,
        CreatePatient(display_name="Meera Shah", phone="+919812345678", email="meera@example.test"),
    )
    zone = ZoneInfo("Asia/Kolkata")
    start = (datetime.now(zone) + timedelta(days=3)).replace(
        hour=10, minute=30, second=0, microsecond=0
    )
    appointments = AppointmentService(uow)
    command = BookAppointment(provider_id=provider.id, patient_id=patient.id, starts_at=start)
    booked = await appointments.book(owner, command, "notify-1")
    # The same request again queues nothing new.
    await appointments.book(owner, command, "notify-1")

    queued = await messages(tenancy, tenant_id)
    assert sorted((row.kind, row.channel) for row in queued) == [
        ("confirmation", "email"),
        ("confirmation", "sms"),
        ("reminder", "email"),
        ("reminder", "sms"),
    ]
    text = next(row for row in queued if row.kind == "confirmation" and row.channel == "sms")
    assert "Meera Shah" in text.body and "10:30 AM" in text.body and "Tenant A" in text.body
    assert text.recipient == "+919812345678" and text.sender == clinic_number
    reminder = next(row for row in queued if row.kind == "reminder" and row.channel == "sms")
    assert reminder.available_at == start.astimezone(UTC) - timedelta(hours=24)

    # The worker sends what is due: texts through the phone service, email through SMTP.
    sent_texts: list[dict[str, list[str]]] = []
    sent_mail: list[dict[str, str]] = []

    def twilio(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/2010-04-01/Accounts/{ACCOUNT}/Messages.json"
        sent_texts.append(parse_qs(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM1"})

    def smtp(_settings: object, payload: dict[str, str], _identity: str) -> None:
        sent_mail.append(payload)

    monkeypatch.setattr(delivery, "send_smtp", smtp)
    assert isinstance(tenancy.settings.worker_database_url, SecretStr)
    settings = tenancy.settings.model_copy(
        update={
            "database_url": tenancy.settings.worker_database_url,
            "twilio_account_sid": ACCOUNT,
            "twilio_auth_token": SecretStr("a" * 32),
            "smtp_host": "smtp.example.test",
            "smtp_sender": "clinic@example.test",
        }
    )

    async def run_worker() -> None:
        # Other tests share this database; only this clinic's messages may go out here.
        async with tenancy.admin.begin() as session:
            await session.execute(
                update(OutboundMessage)
                .where(OutboundMessage.tenant_id != tenant_id, OutboundMessage.status == "queued")
                .values(status="cancelled")
            )
        worker = Database(settings)
        try:
            sender = MessageDelivery(
                worker.session_factory,
                settings,
                TenantIntegrationResolver(
                    worker.session_factory,
                    settings,
                    role_check=verify_worker_role,
                    cache_seconds=0,
                ),
                transport=httpx.MockTransport(twilio),
                retry=RetryPolicy(max_attempts=1),
            )
            while await sender.deliver_one():
                pass
        finally:
            await worker.close()

    await run_worker()
    assert len(sent_texts) == 1 and sent_texts[0]["To"] == ["+919812345678"]
    assert sent_texts[0]["From"] == [clinic_number] and "10:30 AM" in sent_texts[0]["Body"][0]
    assert [mail["to"] for mail in sent_mail] == ["meera@example.test"]

    # Moving it stops the old reminder and tells the patient the new time.
    moved = await appointments.reschedule(
        owner,
        booked.id,
        RescheduleAppointment(starts_at=start + timedelta(hours=1), expected_version=1),
    )
    after_move = await messages(tenancy, tenant_id)
    assert {row.status for row in after_move if row.dedupe_key.endswith(":1:reminder:sms")} == {
        "cancelled"
    }
    await run_worker()
    assert len(sent_texts) == 2 and "11:30 AM" in sent_texts[1]["Body"][0]

    # A patient who opts out of texts gets none of the ones still waiting, nor new ones.
    await PatientService(uow).update(
        owner,
        patient.id,
        UpdatePatient(
            display_name="Meera Shah",
            phone="+919812345678",
            email="meera@example.test",
            sms_opt_out=True,
        ),
    )
    await appointments.cancel(owner, booked.id, CancelAppointment(expected_version=moved.version))
    await run_worker()
    final = await messages(tenancy, tenant_id)
    assert [row.channel for row in final if row.kind == "cancellation"] == ["email"]
    assert not any(row.status == "queued" for row in final)
    assert len(sent_texts) == 2 and len(sent_mail) == 3
    assert "cancelled" in sent_mail[-1]["body"]

    # The clinic's log shows what happened without exposing the full number.
    log = await NotificationService(uow, tenancy.settings).log(owner, 50, 0, channel="sms")
    assert log and all(
        item.recipient.endswith("5678") and "9812" not in item.recipient for item in log
    )
    # A clinic without a number cannot send a test text.
    with pytest.raises(NoClinicNumberError):
        await NotificationService(uow, tenancy.settings).send_test(
            tenancy.context(tenancy.b), TestMessage(channel="sms", to=CLINIC_NUMBER)
        )
