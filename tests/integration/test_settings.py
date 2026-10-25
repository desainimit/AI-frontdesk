"""Clinic settings: defaults, one-click saves, history, and who may change them."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.appointments.availability import AvailabilityService
from app.appointments.commands import BookAppointment, SlotQuery
from app.appointments.service import AppointmentService
from app.audit.models import AuditEvent
from app.auth.context import ActorType
from app.clinic.schemas import ServiceCommand, TimeOffCommand
from app.clinic.service import ClinicService
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import AuthorizationError, ValidationFailedError
from app.locations.schemas import CreateLocation
from app.locations.service import LocationService
from app.main import create_app
from app.patients.schemas import CreatePatient
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider
from app.providers.service import ProviderService
from app.settings.schemas import SettingsSection
from app.settings.service import SettingsService
from app.tenants.models import Tenant
from tests.fakes import FakeDependency
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.integration.workspaces import add_member, member_role


async def test_gujarati_settings_round_trip_and_tenant_isolation(tenancy: TenancyFixture) -> None:
    settings = tenancy.settings.model_copy(
        update={"stt_provider": "azure", "tts_provider": "azure"}
    )
    service = SettingsService(tenancy.database.session_factory, settings)
    owner = tenancy.context(tenancy.a)
    before = (await service.get(owner)).settings.receptionist.model_dump(mode="json")
    saved = await service.save(
        owner,
        SettingsSection.RECEPTIONIST,
        {
            **before,
            "greeting": "નમસ્તે. હું કેવી રીતે મદદ કરી શકું?",
            "languages": {
                "default_language": "en",
                "enabled_languages": ["en", "es", "fr", "de", "pt", "hi", "guj"],
                "auto_detect": True,
            },
        },
    )
    assert saved.settings.receptionist.languages.default_language == "en"
    assert saved.settings.receptionist.languages.auto_detect
    assert "gu" in saved.settings.receptionist.languages.enabled_languages
    assert (await service.get(owner)).settings.receptionist == saved.settings.receptionist
    other = await service.get(tenancy.context(tenancy.b))
    assert other.version == 1 and other.settings.receptionist.languages.default_language == "en"


async def test_http_rejects_incompatible_gujarati_voice_without_saving(
    tenancy: TenancyFixture,
) -> None:
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=tenancy.a.headers
        ) as client,
    ):
        before = (await client.get("/api/v1/settings")).json()
        receptionist = before["settings"]["receptionist"]
        rejected = await client.put(
            "/api/v1/settings/receptionist",
            json={
                **receptionist,
                "languages": {
                    "default_language": "gu",
                    "enabled_languages": ["gu"],
                    "auto_detect": False,
                },
            },
        )
        assert rejected.status_code == 422
        field = rejected.json()["error"]["fields"][0]
        assert field["field"] == "speech_connections.tts" and "Azure Speech" in field["message"]
        after = (await client.get("/api/v1/settings")).json()
        assert after["version"] == before["version"]
        assert after["settings"]["receptionist"] == receptionist


async def test_http_saves_gujarati_with_english_main_and_switching_on(
    tenancy: TenancyFixture,
) -> None:
    config = tenancy.settings.model_copy(update={"stt_provider": "azure", "tts_provider": "azure"})
    app = create_app(config, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=tenancy.a.headers
        ) as client,
    ):
        before = (await client.get("/api/v1/settings")).json()
        languages = {
            "default_language": "en",
            "enabled_languages": ["en", "es", "fr", "de", "pt", "hi", "gu"],
            "auto_detect": True,
        }
        saved = await client.put(
            "/api/v1/settings/receptionist",
            json={
                **before["settings"]["receptionist"],
                "languages": languages,
            },
        )
        assert saved.status_code == 200
        after = (await client.get("/api/v1/settings")).json()
        assert after["settings"]["receptionist"]["languages"] == languages
        assert (
            after["settings"]["receptionist"]["greeting"]
            == before["settings"]["receptionist"]["greeting"]
        )
        assert after["version"] == before["version"] + 1


async def test_gujarati_text_settings_can_save_with_voice_disabled(
    tenancy: TenancyFixture,
) -> None:
    service = SettingsService(tenancy.database.session_factory, tenancy.settings)
    owner = tenancy.context(tenancy.a)
    before = (await service.get(owner)).settings
    await service.save(
        owner,
        SettingsSection.CALLS,
        {
            **before.calls.model_dump(mode="json"),
            "enabled": False,
        },
    )
    await service.save(
        owner,
        SettingsSection.RECEPTIONIST,
        {
            **before.receptionist.model_dump(mode="json"),
            "languages": {
                "default_language": "gu",
                "enabled_languages": ["gu"],
                "auto_detect": False,
            },
        },
    )
    with pytest.raises(ValidationFailedError):
        await service.save(owner, SettingsSection.CALLS, before.calls.model_dump(mode="json"))
    after = await service.get(owner)
    assert not after.settings.calls.enabled
    assert after.settings.receptionist.languages.default_language == "gu"


async def test_a_new_clinic_starts_with_working_settings(tenancy: TenancyFixture) -> None:
    view = await SettingsService(tenancy.database.session_factory).get(tenancy.context(tenancy.a))
    settings = view.settings
    assert view.version == 1
    assert {window.weekday for window in settings.hours.windows} == {0, 1, 2, 3, 4, 5}
    assert settings.clinic.name in settings.receptionist.greeting
    assert "book_appointment" in settings.receptionist.allowed_tools
    assert settings.receptionist.languages.default_language == "en"
    assert settings.calls.enabled and settings.notifications.reminders_enabled
    assert settings.is_open(datetime(2026, 10, 12, 5, 0, tzinfo=UTC))  # Monday 10:30 in Kolkata
    assert not settings.is_open(datetime(2026, 10, 11, 5, 0, tzinfo=UTC))  # Sunday


async def test_saving_a_section_takes_effect_and_is_recorded(tenancy: TenancyFixture) -> None:
    service = SettingsService(tenancy.database.session_factory)
    owner = tenancy.context(tenancy.a)
    current = (await service.get(owner)).settings
    greeting = "Namaste, you have reached the clinic."
    saved = await service.save(
        owner,
        SettingsSection.RECEPTIONIST,
        {**current.receptionist.model_dump(mode="json"), "greeting": greeting, "can_book": False},
    )
    assert saved.version == 2 and saved.settings.receptionist.greeting == greeting
    assert "book_appointment" not in saved.settings.receptionist.allowed_tools
    # Saving the same values again is not a change.
    again = await service.save(
        owner, SettingsSection.RECEPTIONIST, saved.settings.receptionist.model_dump(mode="json")
    )
    assert again.version == 2
    history = await service.history(owner, 10, None)
    assert [item.section for item in history.items] == ["receptionist"]
    assert {change.field for change in history.items[0].changes} == {"greeting", "can_book"}
    assert history.items[0].changed_by == "Admin A" and history.next_cursor is None

    # The clinic name is the workspace name.
    await service.save(
        owner,
        SettingsSection.CLINIC,
        {**current.clinic.model_dump(mode="json"), "name": "Sunrise Clinic"},
    )
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenancy.a.tenant_id)
        assert (await session.get(Tenant, tenancy.a.tenant_id)).name == "Sunrise Clinic"  # pyright: ignore[reportOptionalMemberAccess]
        actions = (
            await session.scalars(
                select(AuditEvent.action).where(AuditEvent.tenant_id == tenancy.a.tenant_id)
            )
        ).all()
    assert {"settings_receptionist_updated", "settings_clinic_updated"} <= set(actions)
    # Another clinic sees none of it.
    other = await service.get(tenancy.context(tenancy.b))
    assert other.version == 1 and other.settings.receptionist.greeting != greeting
    assert (await service.history(tenancy.context(tenancy.b), 10, None)).items == []


async def test_bad_values_and_missing_permission_are_refused(tenancy: TenancyFixture) -> None:
    service = SettingsService(tenancy.database.session_factory)
    owner = tenancy.context(tenancy.a)
    hours = (await service.get(owner)).settings.hours.model_dump(mode="json")
    overlapping: Any = [
        {"weekday": 0, "opens": "09:00", "closes": "12:00"},
        {"weekday": 0, "opens": "11:00", "closes": "13:00"},
    ]
    with pytest.raises(ValidationFailedError) as refused:
        await service.save(owner, SettingsSection.HOURS, {**hours, "windows": overlapping})
    assert "overlap" in refused.value.fields[0][1]
    assert (await service.get(owner)).version == 1

    member_id = uuid4()
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenancy.a.tenant_id)
        await add_member(
            session,
            tenant_id=tenancy.a.tenant_id,
            email=f"{uuid4()}@example.test",
            display_name="Member",
            user_id=member_id,
        )
    member = replace(
        owner,
        actor_id=member_id,
        role_id=member_role(tenancy.a.tenant_id),
        capabilities=frozenset({"tenant:read"}),
    )
    assert member.actor_type == ActorType.USER
    with pytest.raises(AuthorizationError):
        await service.save(member, SettingsSection.HOURS, hours)

    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=tenancy.a.headers
        ) as client,
    ):
        assert (await client.get("/api/v1/settings")).json()["version"] == 1
        rejected = await client.put(
            "/api/v1/settings/hours", json={**hours, "windows": overlapping}
        )
        assert rejected.status_code == 422
        assert rejected.json()["error"]["fields"][0]["message"].startswith("Opening hours")
        saved = await client.put("/api/v1/settings/hours", json={**hours, "holidays": []})
        assert saved.status_code == 200 and saved.json()["version"] == 1
        page = (await client.get("/api/v1/services")).json()
        assert page == {"items": [], "next_cursor": None}


async def test_a_booking_follows_the_provider_schedule_and_service_length(
    tenancy: TenancyFixture,
) -> None:
    uow = UnitOfWork(tenancy.database.session_factory)
    owner = tenancy.context(tenancy.a)
    location = await LocationService(uow).create(owner, CreateLocation(name="Main clinic"))
    provider = await ProviderService(uow).create(
        owner, CreateProvider(location_id=location.id, display_name="Dr. Rao")
    )
    patient = await PatientService(uow).create(
        owner, CreatePatient(display_name="Synthetic Patient", phone="+919999999999")
    )
    clinic = ClinicService(uow)
    # A new provider works the clinic's opening hours.
    schedule = await clinic.schedule(owner, provider.id)
    assert len(schedule.windows) == 6 and schedule.timezone == "Asia/Kolkata"
    long_visit = await clinic.save_service(
        owner, ServiceCommand(name="New patient visit", duration_minutes=45)
    )

    zone = ZoneInfo("Asia/Kolkata")
    today = datetime.now(zone).date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    opening = datetime.combine(monday, datetime.min.time(), zone).replace(hour=9)
    day = SlotQuery(starts_at=opening, ends_at=opening + timedelta(hours=9))
    availability = AvailabilityService(uow)
    assert len(await availability.slots(owner, provider.id, day)) == 18  # 30 minutes each
    assert len(await availability.slots(owner, provider.id, day, long_visit.id)) == 12

    booked = await AppointmentService(uow).book(
        owner,
        BookAppointment(
            provider_id=provider.id,
            patient_id=patient.id,
            service_id=long_visit.id,
            starts_at=opening,
        ),
    )
    assert booked.ends_at - booked.starts_at == timedelta(minutes=45)
    assert len(await availability.slots(owner, provider.id, day, long_visit.id)) == 11
    listed = await AppointmentService(uow).list(owner, 10, 0)
    assert listed[0].service_name == "New patient visit"

    await clinic.add_time_off(
        owner, provider.id, TimeOffCommand(starts_on=monday, ends_on=monday, reason="Conference")
    )
    assert await availability.slots(owner, provider.id, day) == []
