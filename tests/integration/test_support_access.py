"""Real PostgreSQL RLS and HTTP lifecycle tests for platform support sessions."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select, update
from sqlalchemy.exc import DBAPIError

from app.api.platform import login_limit
from app.audit.models import AuditEvent
from app.calls.models import PhoneNumber
from app.calls.service import CallService
from app.database.rls import set_tenant_scope
from app.database.unit_of_work import UnitOfWork
from app.errors import AuthenticationError
from app.events.models import IdempotencyRecord
from app.knowledge.models import Document, DocumentVersion
from app.main import create_app
from app.patients.models import Patient
from app.patients.service import PatientService
from app.platform.bootstrap import seed_super_admin
from app.platform.models import PlatformAuditEvent, SupportSession
from app.platform.service import PlatformAuthenticationService
from app.platform.support import SupportAccessService
from tests.fakes import FakeDependency
from tests.integration.platform_mfa_helpers import mfa_login
from tests.integration.test_domain import seed
from tests.integration.test_platform import seed_command
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_support_can_read_knowledge_when_processing_is_disabled(
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

    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    parent = (
        await mfa_login(
            PlatformAuthenticationService(tenancy.database.session_factory),
            command,
            tenancy.settings,
        )
    ).access_token
    app = create_app(
        tenancy.settings.model_copy(update={"knowledge_enabled": False}),
        redis_factory=lambda _: FakeDependency(),
        rate_limits=False,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        platform_headers = {"Authorization": f"Bearer {parent}"}

        async def enter(capabilities: list[str], *, sensitive: bool = False) -> dict[str, str]:
            response = await client.post(
                "platform/support-sessions",
                json={
                    "tenant_id": str(tenancy.a.tenant_id),
                    "reason": "Reviewing the tenant knowledge document inventory",
                    "capabilities": capabilities,
                    "confirm_sensitive": sensitive,
                },
                headers=platform_headers,
            )
            assert response.status_code == 201, response.text
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        read_headers = await enter(["tenant:read", "knowledge:read"])
        listing = await client.get("knowledge/documents", headers=read_headers)
        assert listing.status_code == 200
        assert [row["id"] for row in listing.json()] == [str(first.id)]
        assert (
            await client.get(f"knowledge/documents/{first.id}/versions", headers=read_headers)
        ).json()[0]["status"] == "ready"
        assert (
            await client.get(f"knowledge/documents/{second.id}/versions", headers=read_headers)
        ).status_code == 404
        assert (
            await client.post(
                "knowledge/documents",
                content=b"# Test",
                headers={**read_headers, "X-File-Name": "test.md"},
            )
        ).status_code == 403

        no_read_headers = await enter(["tenant:read"])
        assert (await client.get("knowledge/documents", headers=no_read_headers)).status_code == 403

        write_headers = await enter(["tenant:read", "knowledge:write"], sensitive=True)
        assert (
            await client.post(
                "knowledge/documents",
                content=b"# Test",
                headers={**write_headers, "X-File-Name": "test.md"},
            )
        ).status_code == 503


async def test_support_scope_isolation_audit_revocation_and_expiry(tenancy: TenancyFixture) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    platform_auth = PlatformAuthenticationService(tenancy.database.session_factory)
    parent = (await mfa_login(platform_auth, command, tenancy.settings)).access_token
    async with tenancy.admin.begin() as session:
        first = Patient(tenant_id=tenancy.a.tenant_id, display_name="A only", phone="+911111111111")
        second = Patient(
            tenant_id=tenancy.b.tenant_id, display_name="B only", phone="+912222222222"
        )
        session.add_all([first, second])
        await session.flush()
        a_patient_id, b_patient_id = first.id, second.id

    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)

    async def allow() -> None:
        pass

    app.dependency_overrides[login_limit] = allow
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        platform_headers = {"Authorization": f"Bearer {parent}"}
        payload: dict[str, Any] = {
            "tenant_id": str(tenancy.a.tenant_id),
            "reason": "Investigating tenant A patient retrieval incident",
            "capabilities": [
                "tenant:read",
                "patient:read",
                "user:read",
                "call:read",
                "audit:read",
                "appointment:read",
                "directory:read",
            ],
        }
        # A reason is enough: no confirmation step guards sensitive capabilities.
        entered = await client.post(
            "platform/support-sessions", json=payload, headers=platform_headers
        )
        assert entered.status_code == 201
        issued = entered.json()
        assert issued["tenant_id"] == str(tenancy.a.tenant_id)
        support_headers = {"Authorization": f"Bearer {issued['access_token']}"}
        stale_context = await SupportAccessService(tenancy.database.session_factory).authenticate(
            issued["access_token"], uuid4()
        )
        current = await client.get("support/me", headers=support_headers)
        assert current.status_code == 200
        assert current.json()["platform_user_id"] == issued["platform_user_id"]
        assert (await client.get("platform/me", headers=support_headers)).status_code == 401
        assert (await client.get("users/me", headers=support_headers)).status_code == 403
        patients = await client.get("patients", headers=support_headers)
        assert patients.status_code == 200
        assert {row["id"] for row in patients.json()} == {str(a_patient_id)}
        for route in ("calls", "follow-ups", "appointments", "locations", "audit-events"):
            response = await client.get(route, headers=support_headers)
            assert response.status_code == 200, (route, response.text)
        assert (
            await client.get(f"patients/{b_patient_id}", headers=support_headers)
        ).status_code == 404
        assert (
            await client.get(
                "patients", headers={**support_headers, "X-Tenant-ID": str(tenancy.b.tenant_id)}
            )
        ).status_code == 403
        assert (
            await client.post(
                "patients",
                json={"display_name": "Forbidden", "phone": "+913333333333"},
                headers=support_headers,
            )
        ).status_code == 403
        assert (await client.get("users", headers=support_headers)).json()[0]["id"] == str(
            tenancy.a.user_id
        )
        session_id = UUID(issued["id"])
        async with tenancy.database.session_factory.begin() as session:
            assert (await session.scalars(select(SupportSession.id))).all() == []
            await set_tenant_scope(session, tenancy.a.tenant_id)
            assert (await session.scalars(select(SupportSession.id))).all() == [session_id]
            assert (await session.scalars(select(Patient.id))).all() == [a_patient_id]
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, tenancy.b.tenant_id)
            assert (await session.scalars(select(SupportSession.id))).all() == []
            assert (await session.scalars(select(Patient.id))).all() == [b_patient_id]
        with pytest.raises(DBAPIError):
            async with tenancy.database.session_factory.begin() as session:
                await set_tenant_scope(session, tenancy.a.tenant_id)
                await session.execute(
                    insert(Patient).values(
                        id=uuid4(),
                        tenant_id=tenancy.b.tenant_id,
                        display_name="Cross tenant write",
                        phone="+914444444444",
                    )
                )
        async with tenancy.admin.begin() as session:
            tenant_audits = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenancy.a.tenant_id,
                        AuditEvent.actor_type == "platform_support",
                    )
                )
            ).all()
            assert tenant_audits
            assert all(
                event.platform_user_id == UUID(issued["platform_user_id"])
                and event.support_session_id == session_id
                and event.actor_id is None
                for event in tenant_audits
            )
            actions = (
                await session.scalars(
                    select(PlatformAuditEvent.action).where(
                        PlatformAuditEvent.support_session_id == session_id
                    )
                )
            ).all()
            assert {"support_enter", "support_action_attempt", "support_action_denied"} <= set(
                actions
            )
            request_event = await session.scalar(
                select(PlatformAuditEvent).where(
                    PlatformAuditEvent.support_session_id == session_id,
                    PlatformAuditEvent.route == "/api/v1/patients",
                    PlatformAuditEvent.http_method == "GET",
                )
            )
            assert request_event and request_event.user_id == UUID(issued["platform_user_id"])
            assert request_event.tenant_id == tenancy.a.tenant_id
        assert (
            await client.post(
                f"platform/support-sessions/{tenancy.a.tenant_id}/{session_id}/revoke",
                headers=platform_headers,
            )
        ).status_code == 204
        assert (await client.get("patients", headers=support_headers)).status_code == 401
        with pytest.raises(AuthenticationError):
            await PatientService(UnitOfWork(tenancy.database.session_factory)).list(
                stale_context, 50, 0
            )

        exited = await client.post(
            "platform/support-sessions", json=payload, headers=platform_headers
        )
        assert exited.status_code == 201
        exit_headers = {"Authorization": f"Bearer {exited.json()['access_token']}"}
        assert (await client.post("support/exit", headers=exit_headers)).status_code == 204
        assert (await client.get("support/me", headers=exit_headers)).status_code == 401

        payload["tenant_id"] = str(tenancy.b.tenant_id)
        entered_b = await client.post(
            "platform/support-sessions", json=payload, headers=platform_headers
        )
        assert entered_b.status_code == 201
        b_headers = {"Authorization": f"Bearer {entered_b.json()['access_token']}"}
        assert {row["id"] for row in (await client.get("patients", headers=b_headers)).json()} == {
            str(b_patient_id)
        }
        async with tenancy.admin.begin() as session:
            await session.execute(
                update(SupportSession)
                .where(SupportSession.id == UUID(entered_b.json()["id"]))
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        assert (await client.get("patients", headers=b_headers)).status_code == 401

        entered_again = await client.post(
            "platform/support-sessions", json=payload, headers=platform_headers
        )
        assert entered_again.status_code == 201
        again_headers = {"Authorization": f"Bearer {entered_again.json()['access_token']}"}
        assert (
            await client.post("platform/auth/logout", headers=platform_headers)
        ).status_code == 204
        assert (await client.get("patients", headers=again_headers)).status_code == 401


async def test_support_can_manage_tenant_with_attributed_writes_and_idempotency(
    tenancy: TenancyFixture,
) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    auth = PlatformAuthenticationService(tenancy.database.session_factory)
    parent = (await mfa_login(auth, command, tenancy.settings)).access_token
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        platform_headers = {"Authorization": f"Bearer {parent}"}
        limited = await client.post(
            "platform/support-sessions",
            json={
                "tenant_id": str(tenancy.a.tenant_id),
                "reason": "Reviewing tenant A configuration and appointment workflow",
                "capabilities": ["tenant:read"],
            },
            headers=platform_headers,
        )
        assert limited.status_code == 201
        limited_headers = {"Authorization": f"Bearer {limited.json()['access_token']}"}
        assert (
            await client.post(
                "patients",
                json={"display_name": "Denied", "phone": "+915555555555"},
                headers=limited_headers,
            )
        ).status_code == 403
        support = await client.post(
            "platform/support-sessions",
            json={
                "tenant_id": str(tenancy.a.tenant_id),
                "reason": "Managing tenant A appointment and patient support incident",
                "capabilities": [
                    "tenant:read",
                    "tenant:update",
                    "directory:read",
                    "directory:write",
                    "patient:read",
                    "patient:write",
                    "appointment:read",
                    "appointment:write",
                    "audit:read",
                ],
                "confirm_sensitive": True,
            },
            headers=platform_headers,
        )
        assert support.status_code == 201, support.text
        support_data = support.json()
        support_headers = {"Authorization": f"Bearer {support_data['access_token']}"}
        updated = await client.put(
            "tenants/current/languages",
            json={"default_language": "en", "enabled_languages": ["en"], "auto_detect": False},
            headers=support_headers,
        )
        assert updated.status_code == 200, updated.text
        renamed = await client.patch(
            "tenants/current", json={"name": "Renamed Clinic"}, headers=support_headers
        )
        assert renamed.status_code == 200 and renamed.json()["name"] == "Renamed Clinic"
        location = await client.post(
            "locations",
            json={"name": "Support Clinic", "timezone": "Asia/Kolkata"},
            headers=support_headers,
        )
        assert location.status_code == 201, location.text
        provider = await client.post(
            "providers",
            json={"location_id": location.json()["id"], "display_name": "Dr Support"},
            headers=support_headers,
        )
        assert provider.status_code == 201, provider.text
        patient = await client.post(
            "patients",
            json={"display_name": "Support Patient", "phone": "+916666666666"},
            headers=support_headers,
        )
        assert patient.status_code == 201, patient.text
        start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
        available = await client.post(
            f"providers/{provider.json()['id']}/availability",
            json={
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=2)).isoformat(),
                "slot_minutes": 30,
            },
            headers=support_headers,
        )
        assert available.status_code == 201, available.text
        booking = {
            "provider_id": provider.json()["id"],
            "patient_id": patient.json()["id"],
            "starts_at": start.isoformat(),
        }
        booking_headers = {**support_headers, "Idempotency-Key": "support-booking-1"}
        first = await client.post("appointments", json=booking, headers=booking_headers)
        assert first.status_code == 201, first.text
        replay = await client.post("appointments", json=booking, headers=booking_headers)
        assert replay.status_code == 201 and replay.json() == first.json()
        b_headers = {"Authorization": f"Bearer {tenancy.b.token.get_secret_value()}"}
        other = await client.post(
            "patients",
            json={"display_name": "B only", "phone": "+917777777777"},
            headers=b_headers,
        )
        assert other.status_code == 201
        assert (
            await client.put(
                f"patients/{other.json()['id']}/verification",
                json={"account_identifier": "cross-tenant"},
                headers=support_headers,
            )
        ).status_code == 404
        assert (
            await client.post(
                "appointments",
                json={**booking, "patient_id": other.json()["id"]},
                headers={**support_headers, "Idempotency-Key": "support-cross-tenant"},
            )
        ).status_code == 404
        async with tenancy.admin.begin() as session:
            idempotency = await session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.support_session_id == UUID(support_data["id"])
                )
            )
            assert idempotency is not None
            assert idempotency.tenant_id == tenancy.a.tenant_id
            assert idempotency.actor_id is None and idempotency.call_id is None
            assert idempotency.platform_user_id == UUID(support_data["platform_user_id"])
            tenant_events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.support_session_id == UUID(support_data["id"]),
                        AuditEvent.action.in_(
                            ["tenant_languages_updated", "patient_created", "appointment_created"]
                        ),
                    )
                )
            ).all()
            assert len(tenant_events) == 3
            assert all(
                event.actor_id is None
                and event.platform_user_id == UUID(support_data["platform_user_id"])
                and event.tenant_id == tenancy.a.tenant_id
                for event in tenant_events
            )
        assert (
            await client.post(
                f"platform/support-sessions/{tenancy.a.tenant_id}/{support_data['id']}/revoke",
                headers=platform_headers,
            )
        ).status_code == 204
        assert (
            await client.post(
                "patients",
                json={"display_name": "No longer allowed", "phone": "+918888888888"},
                headers=support_headers,
            )
        ).status_code == 401


async def test_support_can_issue_call_patient_access_without_becoming_a_tenant_user(
    tenancy: TenancyFixture,
) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    domain = await seed(UnitOfWork(tenancy.database.session_factory), tenancy.context(tenancy.a))
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
    calls = CallService(tenancy.database.session_factory)
    call = await calls.receive(number, "CA" + uuid4().hex, uuid4())
    await calls.start(call, "MZ" + uuid4().hex)
    parent = (
        await mfa_login(
            PlatformAuthenticationService(tenancy.database.session_factory),
            command,
            tenancy.settings,
        )
    ).access_token
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        response = await client.post(
            "platform/support-sessions",
            json={
                "tenant_id": str(tenancy.a.tenant_id),
                "reason": "Helping receptionist verify an active patient call",
                "capabilities": ["patient:write"],
                "confirm_sensitive": True,
            },
            headers={"Authorization": f"Bearer {parent}"},
        )
        assert response.status_code == 201
        issued = response.json()
        token_headers = {"Authorization": f"Bearer {issued['access_token']}"}
        access = await client.post(
            f"calls/{call.call_id}/patient-access",
            json={"patient_id": str(domain.patient_id), "verification_attested": True},
            headers=token_headers,
        )
        assert access.status_code == 201, access.text
        assert len(access.json()["code"]) == 8
        assert (
            await client.post(
                f"calls/{call.call_id}/patient-access",
                json={"patient_id": str(domain.patient_id), "verification_attested": False},
                headers=token_headers,
            )
        ).status_code == 403
    async with tenancy.admin.begin() as session:
        event = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenancy.a.tenant_id,
                AuditEvent.action == "call_patient_verification_attested",
                AuditEvent.support_session_id == UUID(issued["id"]),
            )
        )
        assert event is not None
        assert event.actor_id is None and event.platform_user_id == UUID(issued["platform_user_id"])
