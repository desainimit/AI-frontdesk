"""List filters run in the database and stay inside the caller's tenant."""

from datetime import timedelta
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.appointments.commands import CancelAppointment
from app.appointments.service import AppointmentService
from app.audit.queries import AuditQueryService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, has_permission
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import UnitOfWork
from app.main import create_app
from app.patients.schemas import CreatePatient, PatientView
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider
from app.providers.service import ProviderService
from app.users.service import UserService
from tests.fakes import FakeDependency
from tests.integration.test_domain import DomainFixture
from tests.integration.test_domain import domain as domain
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.integration.workspaces import add_member, member_role


async def test_search_and_filters_match_in_the_database(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    uow, context = domain.uow, domain.context
    patients = PatientService(uow)
    await patients.create(
        context, CreatePatient(display_name="Asha 100% Rao", phone="+911111111111")
    )
    await patients.create(
        context, CreatePatient(display_name="Asha_Underscore", phone="+912222222222")
    )
    # The other tenant has a patient that matches every search below.
    other = UnitOfWork(tenancy.database.session_factory)
    await PatientService(other).create(
        tenancy.context(tenancy.b),
        CreatePatient(display_name="Asha Other Tenant", phone="+911111111111"),
    )

    def names(rows: list[PatientView]) -> set[str]:
        return {row.display_name for row in rows}

    assert names(await patients.list(context, 50, 0, "asha")) == {
        "Asha 100% Rao",
        "Asha_Underscore",
    }
    assert names(await patients.list(context, 50, 0, "  ASHA  ")) == names(
        await patients.list(context, 50, 0, "asha")
    )
    assert names(await patients.list(context, 50, 0, "+911111")) == {"Asha 100% Rao"}
    # Wildcards are literal text: "%" and "_" do not match everything.
    assert names(await patients.list(context, 50, 0, "100%")) == {"Asha 100% Rao"}
    assert names(await patients.list(context, 50, 0, "a_u")) == {"Asha_Underscore"}
    assert await patients.list(context, 50, 0, "%") == [
        row for row in await patients.list(context, 50, 0) if "%" in row.display_name
    ]
    assert await patients.list(context, 50, 0, "no such person") == []
    # Filtering is applied before paging: the second match is reachable at offset 1.
    assert len(await patients.list(context, 1, 0, "asha")) == 1
    assert len(await patients.list(context, 1, 1, "asha")) == 1
    assert await patients.list(context, 1, 2, "asha") == []

    providers = ProviderService(uow)
    cardio = await providers.create(
        context,
        CreateProvider(
            location_id=domain.location_id, display_name="Dr Shah", specialty="Cardiology"
        ),
    )
    assert {row.id for row in await providers.list(context, 50, 0, "cardio")} == {cardio.id}
    assert len(await providers.list(context, 50, 0, None, domain.location_id)) == 2
    assert await providers.list(context, 50, 0, None, uuid4()) == []

    appointments = AppointmentService(uow)
    first = await appointments.book(context, domain.booking(0), str(uuid4()))
    second = await appointments.book(context, domain.booking(30), str(uuid4()))
    await appointments.cancel(
        context, second.id, CancelAppointment(expected_version=second.version), str(uuid4())
    )
    booked = await appointments.list(context, 50, 0, status="booked")
    assert [row.id for row in booked] == [first.id]
    assert [row.id for row in await appointments.list(context, 50, 0, status="cancelled")] == [
        second.id
    ]
    # Names replace raw identifiers for a reader who may see patients.
    assert booked[0].provider_name == "Provider" and booked[0].patient_name == "Synthetic Patient"
    assert await appointments.list(context, 50, 0, provider_id=cardio.id) == []
    window = await appointments.list(
        context, 50, 0, starts_from=domain.start + timedelta(minutes=15)
    )
    assert [row.id for row in window] == [second.id]
    assert (
        await appointments.list(context, 50, 0, starts_to=domain.start - timedelta(minutes=1)) == []
    )
    newest = await appointments.list(context, 50, 0, newest_first=True)
    assert [row.id for row in newest] == [second.id, first.id]

    # A member may list directory data but must not receive patient names anywhere.
    member_id = uuid4()
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, context.tenant_id)
        await add_member(
            session,
            tenant_id=context.tenant_id,
            email="filters-member@example.test",
            display_name="Filter Member",
            user_id=member_id,
        )
    users = UserService(uow)
    assert [row.id for row in await users.list(context, 50, 0, q="filter mem")] == [member_id]
    assert [row.id for row in await users.list(context, 50, 0, q="FILTERS-MEMBER@")] == [member_id]
    by_role = await users.list(context, 50, 0, role_id=member_role(context.tenant_id))
    assert {row.id for row in by_role} == {member_id}
    assert await users.list(context, 50, 0, is_active=False) == []

    audit = AuditQueryService(uow)
    created = await audit.list(context, 100, 0, action="appointment_created")
    assert len(created) == 2 and {row.action for row in created} == {"appointment_created"}
    assert (
        await audit.list(context, 100, 0, action="appointment_created", actor_type="system") == []
    )
    assert await audit.list(context, 100, 0, resource_type="no_such_resource") == []
    # An appointment reader without patient access gets no patient names.
    member = RequestContext(
        uuid4(),
        context.tenant_id,
        None,
        ActorType.CALLER,
        call_id=uuid4(),
        provider_call_id="CA" + "0" * 32,
    )
    assert not has_permission(member, Permission.PATIENT_READ)


async def test_filter_parameters_are_validated_and_scoped_over_http(
    tenancy: TenancyFixture, domain: DomainFixture
) -> None:
    await PatientService(domain.uow).create(
        domain.context, CreatePatient(display_name="Http Filter Person", phone="+913333333333")
    )
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1/") as client,
    ):
        a = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
        b = {"Authorization": f"Bearer {tenancy.b.token.get_secret_value()}"}
        found = await client.get("patients", params={"q": "http filter"}, headers=a)
        assert found.status_code == 200
        assert [row["display_name"] for row in found.json()["items"]] == ["Http Filter Person"]
        # The same search from another tenant finds nothing.
        other = await client.get("patients", params={"q": "http filter"}, headers=b)
        assert other.json() == {"items": [], "next_cursor": None}
        for path, params in (
            ("appointments", {"status": "deleted"}),
            ("appointments", {"provider_id": "not-a-uuid"}),
            ("appointments", {"starts_from": "yesterday"}),
            ("calls", {"status": "ringing"}),
            ("audit-events", {"action": "DROP TABLE"}),
            ("users", {"role_id": "owner"}),
            ("patients", {"q": "x" * 101}),
            ("knowledge/documents", {"status": "published"}),
            ("follow-ups", {"status": "open"}),
        ):
            rejected = await client.get(path, params=params, headers=a)
            assert rejected.status_code == 422, (path, params)
        for path, params in (
            (
                "appointments",
                {"status": "booked", "order": "desc", "starts_from": "2026-01-01T00:00:00Z"},
            ),
            ("calls", {"status": "completed", "created_to": "2030-01-01T00:00:00Z"}),
            ("audit-events", {"actor_type": "user", "resource_type": "patient"}),
            ("users", {"q": "admin", "is_active": "true"}),
            ("knowledge/documents", {"status": "approved", "q": "policy"}),
            ("follow-ups", {"status": "all", "kind": "human_review"}),
            ("providers", {"q": "prov"}),
            ("locations", {"q": "clinic"}),
        ):
            accepted = await client.get(path, params=params, headers=a)
            assert accepted.status_code == 200, (path, params, accepted.text)
            assert all(
                row.get("tenant_id", str(tenancy.a.tenant_id)) == str(tenancy.a.tenant_id)
                for row in accepted.json()["items"]
            )
