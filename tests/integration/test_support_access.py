"""A platform administrator working inside a workspace, over HTTP and real row-level security."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError

from app.audit.models import AuditEvent
from app.calls.models import PhoneNumber
from app.calls.service import CallService
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import AuthenticationError
from app.events.models import IdempotencyRecord
from app.identity.models import AccountSession
from app.identity.sessions import authenticate_workspace
from app.knowledge.models import Document, DocumentVersion
from app.main import create_app
from app.patients.models import Patient
from app.patients.service import PatientService
from app.platform.models import PlatformAuditEvent
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness
from tests.integration.test_domain import seed
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


def application(tenancy: TenancyFixture):
    return create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)


async def open_workspace(client: AsyncClient, tenant_id: UUID, reason: str) -> dict[str, str]:
    response = await client.post(f"platform/workspaces/{tenant_id}/open", json={"reason": reason})
    assert response.status_code == 200, response.text
    return response.json()


async def test_administrator_reads_and_adds_knowledge_in_the_opened_workspace(
    tenancy: TenancyFixture,
) -> None:
    first = Document(
        tenant_id=tenancy.a.tenant_id,
        name="Tenant A guide.md",
        source_type="md",
        status="approved",
        active_version=1,
    )
    second = Document(
        tenant_id=tenancy.b.tenant_id,
        name="Tenant B private.md",
        source_type="md",
        status="approved",
        active_version=1,
    )
    async with tenancy.admin.begin() as session:
        session.add_all([first, second])
        await session.flush()
        session.add(
            DocumentVersion(
                tenant_id=tenancy.a.tenant_id,
                document_id=first.id,
                version=1,
                checksum="a" * 64,
                name=first.name,
                source_type="md",
                storage_key="test-only",
                status="ready",
            )
        )
    admin = await IdentityHarness(tenancy).admin()
    app = application(tenancy)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1/", headers=admin.headers
        ) as client,
    ):
        # A reason is required, and nothing is reachable before a workspace is opened.
        assert (await client.get("knowledge/documents")).status_code == 409
        for invalid in ({}, {"reason": "  "}, {"reason": "ok", "capabilities": ["patient:read"]}):
            assert (
                await client.post(f"platform/workspaces/{tenancy.a.tenant_id}/open", json=invalid)
            ).status_code == 422
        assert (
            await client.post(f"platform/workspaces/{uuid4()}/open", json={"reason": "Unknown"})
        ).status_code == 404
        await open_workspace(client, tenancy.a.tenant_id, "Reviewing the knowledge documents")
        listing = await client.get("knowledge/documents")
        assert listing.status_code == 200
        assert [row["id"] for row in listing.json()["items"]] == [str(first.id)]
        versions = await client.get(f"knowledge/documents/{first.id}/versions")
        assert versions.json()[0]["status"] == "ready"
        # The other workspace's document does not exist from here.
        assert (await client.get(f"knowledge/documents/{second.id}/versions")).status_code == 404
        uploaded = await client.post(
            "knowledge/documents", content=b"# Test", headers={"X-File-Name": "test.md"}
        )
        assert uploaded.status_code == 202


async def test_visit_is_scoped_to_one_workspace_attributed_and_ends(
    tenancy: TenancyFixture,
) -> None:
    harness = IdentityHarness(tenancy)
    admin = await harness.admin()
    async with tenancy.admin.begin() as session:
        first = Patient(tenant_id=tenancy.a.tenant_id, display_name="A only", phone="+911111111111")
        second = Patient(
            tenant_id=tenancy.b.tenant_id, display_name="B only", phone="+912222222222"
        )
        session.add_all([first, second])
        await session.flush()
        a_patient_id, b_patient_id = first.id, second.id

    app = application(tenancy)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1/", headers=admin.headers
        ) as client,
    ):
        visit = await open_workspace(client, tenancy.a.tenant_id, "Investigating a booking issue")
        visit_id = UUID(visit["id"])
        assert visit["reason"] == "Investigating a booking issue"
        me = (await client.get("auth/me")).json()
        assert me["workspace"]["id"] == str(tenancy.a.tenant_id)
        assert me["workspace"]["role_name"] == "Platform administrator"
        assert me["workspace"]["user_id"] is None  # Never a member of the workspace.
        assert me["workspace"]["support"]["reason"] == "Investigating a booking issue"
        stale_context = await authenticate_workspace(
            tenancy.database.session_factory, admin.token.get_secret_value(), uuid4()
        )

        patients = await client.get("patients")
        assert patients.status_code == 200
        assert {row["id"] for row in patients.json()["items"]} == {str(a_patient_id)}
        assert patients.headers["cache-control"] == "no-store"
        for route in ("calls", "follow-ups", "appointments", "locations", "audit-events", "users"):
            response = await client.get(route)
            assert response.status_code == 200, (route, response.text)
        assert (await client.get(f"patients/{b_patient_id}")).status_code == 404
        # Naming another workspace in a header changes nothing.
        named = await client.get("patients", headers={"X-Tenant-ID": str(tenancy.b.tenant_id)})
        assert {row["id"] for row in named.json()["items"]} == {str(a_patient_id)}
        # A route with no decision for visiting administrators is refused and recorded.
        assert (await client.get("users/me")).status_code == 403
        created = await client.post(
            "patients", json={"display_name": "Added by support", "phone": "+913333333333"}
        )
        assert created.status_code == 201
        # The platform console stays available while a workspace is open.
        assert (await client.get("platform/tenants?limit=1")).status_code == 200

        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, tenancy.b.tenant_id)
            assert (await session.scalars(select(Patient.id))).all() == [b_patient_id]
        with pytest.raises(DBAPIError):
            async with tenancy.database.session_factory.begin() as session:
                await set_tenant_scope(session, tenancy.a.tenant_id)
                await session.execute(
                    insert(Patient).values(
                        id=uuid4(),
                        tenant_id=tenancy.b.tenant_id,
                        display_name="Cross workspace write",
                        phone="+914444444444",
                    )
                )
        async with tenancy.admin.begin() as session:
            workspace_events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenancy.a.tenant_id,
                        AuditEvent.actor_type == "platform_support",
                    )
                )
            ).all()
            assert "patient_created" in {event.action for event in workspace_events}
            # Written as the administrator's own account, never as a member.
            assert all(
                event.platform_user_id == admin.account_id
                and event.support_session_id == visit_id
                and event.actor_id is None
                for event in workspace_events
            )
            actions = (
                await session.scalars(
                    select(PlatformAuditEvent.action).where(
                        PlatformAuditEvent.support_session_id == visit_id
                    )
                )
            ).all()
            assert {"support_enter", "support_action_attempt", "support_action_denied"} <= set(
                actions
            )
            request_event = await session.scalar(
                select(PlatformAuditEvent).where(
                    PlatformAuditEvent.support_session_id == visit_id,
                    PlatformAuditEvent.route == "/api/v1/patients",
                    PlatformAuditEvent.http_method == "GET",
                )
            )
            assert request_event and request_event.user_id == admin.account_id
            assert request_event.tenant_id == tenancy.a.tenant_id

        # Leaving ends the visit at once, including for a request already in flight.
        left = await client.post("auth/workspace/leave")
        assert left.status_code == 200 and left.json()["workspace"] is None
        blocked = await client.get("patients")
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "workspace_required"
        with pytest.raises(AuthenticationError):
            await PatientService(UnitOfWork(tenancy.database.session_factory)).list(
                stale_context, 50, 0
            )

        # One workspace at a time: opening another replaces the first.
        await open_workspace(client, tenancy.a.tenant_id, "Back in A")
        second_visit = await open_workspace(client, tenancy.b.tenant_id, "Now helping B")
        assert second_visit["id"] != visit["id"]
        visible = (await client.get("patients")).json()["items"]
        assert {row["id"] for row in visible} == {str(b_patient_id)}
        # A visit ends by itself after its time is up.
        expires = datetime.fromisoformat(second_visit["expires_at"])
        assert timedelta(0) < expires - datetime.now(UTC) <= timedelta(minutes=30)
        async with tenancy.admin.begin() as session:
            await session.execute(
                update(AccountSession)
                .where(AccountSession.id == admin.session_id)
                .values(support_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        assert (await client.get("patients")).status_code == 409
        assert (await client.get("auth/me")).json()["workspace"] is None
        assert (await client.get("platform/tenants?limit=1")).status_code == 200

        # Signing out ends everything.
        await open_workspace(client, tenancy.b.tenant_id, "Once more")
        assert (await client.post("auth/logout")).status_code == 204
        assert (await client.get("patients")).status_code == 401
    async with tenancy.admin.begin() as session:
        exits = (
            await session.scalars(
                select(PlatformAuditEvent.action).where(
                    PlatformAuditEvent.user_id == admin.account_id,
                    PlatformAuditEvent.action.in_(["support_enter", "support_exit"]),
                )
            )
        ).all()
        assert exits.count("support_enter") == 4 and exits.count("support_exit") == 1
    # A workspace member cannot open anything.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        refused = await client.post(
            f"platform/workspaces/{tenancy.b.tenant_id}/open",
            json={"reason": "Not an administrator"},
            headers=tenancy.a.headers,
        )
        assert refused.status_code == 403


async def test_administrator_changes_are_attributed_and_safe_to_retry(
    tenancy: TenancyFixture,
) -> None:
    admin = await IdentityHarness(tenancy).admin()
    app = application(tenancy)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1/", headers=admin.headers
        ) as client,
    ):
        visit = await open_workspace(client, tenancy.a.tenant_id, "Fixing an appointment")
        visit_id = UUID(visit["id"])
        clinic = (await client.get("settings")).json()["settings"]["clinic"]
        renamed = await client.put("settings/clinic", json={**clinic, "name": "Renamed Clinic"})
        assert renamed.status_code == 200, renamed.text
        assert (await client.get("tenants/current")).json()["name"] == "Renamed Clinic"
        history = (await client.get("settings/history")).json()["items"]
        assert history[0]["changed_by"] == "AI Frontdesk support"
        location = await client.post(
            "locations", json={"name": "Support Clinic", "timezone": "Asia/Kolkata"}
        )
        assert location.status_code == 201, location.text
        provider = await client.post(
            "providers", json={"location_id": location.json()["id"], "display_name": "Dr Support"}
        )
        assert provider.status_code == 201, provider.text
        patient = await client.post(
            "patients", json={"display_name": "Support Patient", "phone": "+916666666666"}
        )
        assert patient.status_code == 201, patient.text
        # 09:00 UTC is 14:30 in Kolkata; the provider works two hours from then that day.
        start = (datetime.now(UTC) + timedelta(days=3)).replace(
            hour=9, minute=0, second=0, microsecond=0
        )
        weekday = start.astimezone(ZoneInfo("Asia/Kolkata")).weekday()
        schedule = await client.put(
            f"providers/{provider.json()['id']}/schedule",
            json={"windows": [{"weekday": weekday, "starts": "14:30", "ends": "16:30"}]},
        )
        assert schedule.status_code == 200, schedule.text
        booking = {
            "provider_id": provider.json()["id"],
            "patient_id": patient.json()["id"],
            "starts_at": start.isoformat(),
        }
        key = {"Idempotency-Key": "support-booking-1"}
        first = await client.post("appointments", json=booking, headers=key)
        assert first.status_code == 201, first.text
        replay = await client.post("appointments", json=booking, headers=key)
        assert replay.status_code == 201 and replay.json() == first.json()
        other = await client.post(
            "patients",
            json={"display_name": "B only", "phone": "+917777777777"},
            headers=tenancy.b.headers,
        )
        assert other.status_code == 201
        # Another workspace's patient cannot be reached or booked from this visit.
        assert (
            await client.put(
                f"patients/{other.json()['id']}/verification",
                json={"account_identifier": "cross-workspace"},
            )
        ).status_code == 404
        assert (
            await client.post(
                "appointments",
                json={**booking, "patient_id": other.json()["id"]},
                headers={"Idempotency-Key": "support-cross-workspace"},
            )
        ).status_code == 404
        async with tenancy.admin.begin() as session:
            idempotency = await session.scalar(
                select(IdempotencyRecord).where(IdempotencyRecord.support_session_id == visit_id)
            )
            assert idempotency is not None
            assert idempotency.tenant_id == tenancy.a.tenant_id
            assert idempotency.actor_id is None and idempotency.call_id is None
            assert idempotency.platform_user_id == admin.account_id
            events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.support_session_id == visit_id,
                        AuditEvent.action.in_(
                            ["settings_clinic_updated", "patient_created", "appointment_created"]
                        ),
                    )
                )
            ).all()
            assert len(events) == 3
            assert all(
                event.actor_id is None
                and event.platform_user_id == admin.account_id
                and event.tenant_id == tenancy.a.tenant_id
                for event in events
            )
        assert (await client.post("auth/workspace/leave")).status_code == 200
        assert (
            await client.post(
                "patients", json={"display_name": "No longer allowed", "phone": "+918888888888"}
            )
        ).status_code == 409


async def test_administrator_can_issue_call_patient_access_without_becoming_a_member(
    tenancy: TenancyFixture,
) -> None:
    admin = await IdentityHarness(tenancy).admin()
    domain = await seed(UnitOfWork(tenancy.database.session_factory), tenancy.context(tenancy.a))
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
    calls = CallService(tenancy.database.session_factory)
    call = await calls.receive(number, "CA" + uuid4().hex, uuid4())
    await calls.start(call, "MZ" + uuid4().hex)
    app = application(tenancy)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test/api/v1/", headers=admin.headers
        ) as client,
    ):
        visit = await open_workspace(client, tenancy.a.tenant_id, "Helping verify a caller")
        access = await client.post(
            f"calls/{call.call_id}/patient-access",
            json={"patient_id": str(domain.patient_id), "verification_attested": True},
        )
        assert access.status_code == 201, access.text
        assert len(access.json()["code"]) == 8
        assert (
            await client.post(
                f"calls/{call.call_id}/patient-access",
                json={"patient_id": str(domain.patient_id), "verification_attested": False},
            )
        ).status_code == 403
    async with tenancy.admin.begin() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenancy.a.tenant_id,
                AuditEvent.action == "call_patient_verification_attested",
                AuditEvent.support_session_id == UUID(visit["id"]),
            )
        )
        assert event is not None
        assert event.actor_id is None and event.platform_user_id == admin.account_id
