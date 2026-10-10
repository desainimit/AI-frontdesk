"""Creating a clinic in one step: defaults, owner invitation, phone number, going live."""

import secrets
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text

from app.clinic.models import Service
from app.core.database.rls import set_tenant_scope
from app.events.idempotency import IdempotencyConflictError
from app.locations.models import Location
from app.main import create_app
from app.platform.defaults import WorkspaceDefaultsService, product_defaults
from app.platform.workspaces import NewWorkspace, Owner, OwnerNotChangeableError
from app.rbac.models import TenantRole
from app.settings.models import TenantSettings
from app.tenants.getting_started import GettingStartedService
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness, mail_settings
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

ACCOUNT = "AC" + "d" * 32
NUMBER_SID = "PN" + "e" * 32
CALLS_ADDRESS = "https://calls.example.test"


async def test_a_new_clinic_is_complete_and_a_retry_creates_nothing(
    tenancy: TenancyFixture,
) -> None:
    harness = IdentityHarness(tenancy)
    platform = await harness.platform()
    email = f"{uuid4()}@example.test"
    command = NewWorkspace.model_validate(
        {
            "name": "Sunrise Clinic",
            "timezone": "Asia/Dubai",
            "address": "12 Lake Road",
            "phone": "+97141234567",
            "owner": {"email": email.upper(), "display_name": "Dr Owner"},
        }
    )
    key = str(uuid4())
    created = await harness.workspaces.create(platform, command, key)
    clinic = created.workspace
    assert created.warnings == [] and clinic.service_state == "setup"
    assert clinic.timezone == "Asia/Dubai" and clinic.phone_numbers == []
    assert clinic.owner is not None
    assert clinic.owner.email == email and clinic.owner.invitation == "queued"

    again = await harness.workspaces.create(platform, command, key)
    assert again.workspace.id == clinic.id
    with pytest.raises(IdempotencyConflictError):
        await harness.workspaces.create(
            platform, command.model_copy(update={"name": "Another name"}), key
        )

    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, clinic.id)
        document = await session.scalar(
            select(TenantSettings.document).where(TenantSettings.tenant_id == clinic.id)
        )
        assert document is not None
        assert document["clinic"] == {
            **document["clinic"],
            "name": "Sunrise Clinic",
            "timezone": "Asia/Dubai",
            "address": "12 Lake Road",
            "phone": "+97141234567",
        }
        assert "Sunrise Clinic" in document["receptionist"]["greeting"]
        assert len(document["hours"]["windows"]) == 6
        roles = (
            await session.scalars(select(TenantRole.name).where(TenantRole.tenant_id == clinic.id))
        ).all()
        assert sorted(roles) == ["Admin", "Front desk", "Owner", "Viewer"]
        services = (
            await session.scalars(select(Service.name).where(Service.tenant_id == clinic.id))
        ).all()
        assert sorted(services) == ["Follow-up visit", "New patient visit"]
        location = await session.scalar(select(Location).where(Location.tenant_id == clinic.id))
        assert location is not None and location.address == "12 Lake Road"

    # The owner's name can be corrected until they accept.
    changed = await harness.workspaces.change_owner(
        platform, clinic.id, Owner(email=email, display_name="Dr A. Owner")
    )
    assert changed.owner is not None and changed.owner.display_name == "Dr A. Owner"
    session_view = await harness.accept(await harness.link_token(user_id=clinic.owner.user_id))
    assert session_view is not None and session_view.next == "workspace"
    with pytest.raises(OwnerNotChangeableError):
        await harness.workspaces.change_owner(
            platform, clinic.id, Owner(email=email, display_name="Too late")
        )
    assert await harness.invitation_status(platform, clinic.id) == "accepted"

    # The owner's first screen knows what is done and what is left.
    owner = await harness.context(session_view.access_token)
    steps = {
        step.key: step.done
        for step in (await GettingStartedService(harness.sessions).get(owner)).steps
    }
    assert steps["clinic_details"] and steps["appointment_types"]
    assert not steps["provider"] and not steps["phone_number"] and not steps["live"]


async def test_creation_can_assign_a_number_and_go_live(tenancy: TenancyFixture) -> None:
    number = "+1" + str(uuid4().int)[:10]
    updates: list[dict[str, list[str]]] = []

    def twilio(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            updates.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={})
        owned: list[dict[str, Any]] = [
            {"sid": NUMBER_SID, "phone_number": number, "account_sid": ACCOUNT}
        ]
        return httpx.Response(200, json={"incoming_phone_numbers": owned})

    # Every provider account the platform needs, given in code so no database row is used.
    settings = mail_settings(tenancy.settings).model_copy(
        update={
            "telephony_public_base_url": CALLS_ADDRESS,
            "stream_token_key": SecretStr(secrets.token_urlsafe(48)),
            "twilio_account_sid": ACCOUNT,
            "twilio_auth_token": SecretStr("f" * 32),
            "deepgram_api_key": SecretStr("k" * 32),
            "elevenlabs_api_key": SecretStr("k" * 32),
            "gemini_api_key": SecretStr("k" * 32),
        }
    )
    admin = await IdentityHarness(tenancy).admin()
    app = create_app(settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test/api/v1/platform/",
            headers=admin.headers,
        ) as client,
    ):
        app.state.provider_transport = httpx.MockTransport(twilio)
        body = {
            "name": "Live Clinic",
            "owner": {"email": f"{uuid4()}@example.test", "display_name": "Owner"},
            "phone_number": number,
            "go_live": True,
        }
        key = {"Idempotency-Key": str(uuid4())}
        created = await client.post("workspaces", json=body, headers=key)
        assert created.status_code == 201, created.text
        result = created.json()
        assert result["warnings"] == []
        assert result["workspace"]["service_state"] == "live"
        assert result["workspace"]["phone_numbers"] == [number]
        assert result["workspace"]["owner"]["invitation"] == "queued"
        assert updates[0]["VoiceUrl"] == [f"{CALLS_ADDRESS}/webhooks/v1/twilio/incoming"]
        # The same request again answers with the same clinic and touches nothing.
        replay = await client.post("workspaces", json=body, headers=key)
        assert replay.json()["workspace"]["id"] == result["workspace"]["id"] and len(updates) == 1
        detail = await client.get(f"workspaces/{result['workspace']['id']}")
        assert detail.status_code == 200 and detail.json()["phone_numbers"] == [number]

        # A number the account does not own: the clinic is created, the rest is reported.
        other = await client.post(
            "workspaces",
            json={**body, "name": "Second Clinic", "phone_number": "+15550001111"},
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert other.status_code == 201
        steps = {warning["step"] for warning in other.json()["warnings"]}
        assert steps == {"phone_number", "go_live"}
        assert other.json()["workspace"]["service_state"] == "setup"
        # Only platform administrators create clinics.
        refused = await client.post(
            "workspaces", json=body, headers={**tenancy.a.headers, "Idempotency-Key": "x"}
        )
        assert refused.status_code == 403


async def test_administrators_can_change_what_new_clinics_start_with(
    tenancy: TenancyFixture,
) -> None:
    harness = IdentityHarness(tenancy)
    platform = await harness.platform()
    service = WorkspaceDefaultsService(harness.sessions)
    assert (await service.get(platform)).customised is False
    custom = product_defaults()
    custom.receptionist.greeting = "Welcome to {{clinic_name}}, your health partner."
    custom.services = custom.services[:1]
    try:
        saved = await service.save(platform, custom)
        assert saved.customised and len(saved.defaults.services) == 1
        clinic = await harness.onboard(platform, f"{uuid4()}@example.test")
        async with tenancy.admin.begin() as session:
            await set_tenant_scope(session, clinic.tenant_id)
            greeting = await session.scalar(
                text(
                    "SELECT document->'receptionist'->>'greeting' FROM tenant_settings "
                    "WHERE tenant_id = :id"
                ),
                {"id": clinic.tenant_id},
            )
            count = await session.scalar(
                text("SELECT count(*) FROM services WHERE tenant_id = :id"),
                {"id": clinic.tenant_id},
            )
        assert greeting is not None and greeting.startswith("Welcome to Clinic ")
        assert count == 1
    finally:
        # Shared by every test in this database.
        assert (await service.save(platform, None)).customised is False
