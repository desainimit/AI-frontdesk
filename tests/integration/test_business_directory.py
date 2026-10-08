"""Directory revision safety, isolation, normalized records and published runtime facts."""

import base64
import json
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.business.models import BusinessService, DirectoryMutation, ServiceStaff
from app.business.persistence import sync_directory
from app.business.schemas import (
    BusinessListQuery,
    DirectorySnapshot,
    DirectoryWrite,
    ServiceDefinition,
    StaffDefinition,
)
from app.business.service import BusinessDirectoryService
from app.calls.directory import CallDirectoryService, DirectoryQuery
from app.calls.models import Call, PhoneNumber
from app.calls.service import CallContext
from app.configuration.models import ConfigurationDraft
from app.configuration.schemas import SaveConfiguration
from app.configuration.service import ConfigurationService
from app.database.rls import set_tenant_scope
from app.errors import ConflictError
from app.main import create_app
from app.rbac.service import RevisionConflictError
from app.security.encryption import SecretCipher
from tests.fakes import FakeDependency
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_directory_atomic_retries_isolation_and_rollback(tenancy: TenancyFixture) -> None:
    a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
    directory = BusinessDirectoryService(tenancy.database.session_factory)
    configuration = ConfigurationService(tenancy.database.session_factory)
    draft = await directory.get_directory(a)
    staff = StaffDefinition(id=uuid4(), name="Reception staff", email="private@example.test")
    command = DirectoryWrite(
        request_id=uuid4(), draft_revision=draft.draft_revision, expected_revision=0, record=staff
    )
    saved = await directory.write(a, command)
    assert (await directory.write(a, command)) == saved
    with pytest.raises(ConflictError):
        await directory.write(
            a, command.model_copy(update={"record": staff.model_copy(update={"name": "Changed"})})
        )
    service = ServiceDefinition(
        id=uuid4(), name="Consultation", price_minor=150000, staff_ids=[staff.id]
    )
    created = await directory.write(
        a,
        DirectoryWrite(
            request_id=uuid4(),
            draft_revision=saved.draft_revision,
            expected_revision=0,
            record=service,
        ),
    )
    first = await configuration.publish(a, created.draft_revision)
    changed = await directory.write(
        a,
        DirectoryWrite(
            request_id=uuid4(),
            draft_revision=created.draft_revision,
            expected_revision=1,
            record=service.model_copy(update={"price_minor": 200000}),
        ),
    )
    assert (await configuration.effective(a)).document.directory.services[0].price_minor == 150000
    with pytest.raises(RevisionConflictError):
        await directory.write(
            a,
            DirectoryWrite(
                request_id=uuid4(),
                draft_revision=created.draft_revision,
                expected_revision=1,
                record=service,
            ),
        )
    latest = await configuration.get(a)
    altered = latest.document.model_copy(deep=True)
    altered.directory.services = []
    with pytest.raises(ConflictError):
        await configuration.save(a, SaveConfiguration(revision=latest.revision, document=altered))
    with pytest.raises(ConflictError):
        await directory.write(
            b,
            DirectoryWrite(
                request_id=uuid4(),
                draft_revision=1,
                expected_revision=0,
                record=ServiceDefinition(id=uuid4(), name="Foreign", staff_ids=[staff.id]),
            ),
        )
    assert not (await directory.get_directory(b)).records.services
    restored = await configuration.rollback(a, first.version, changed.draft_revision)
    assert restored.document.directory.services[0].price_minor == 150000
    view = await directory.get_directory(a)
    assert view.revisions[service.id] > changed.entity_revision
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, a.tenant_id)
        row = await session.get(BusinessService, service.id)
        assert row is not None and row.price_minor == 150000
        assert len((await session.scalars(select(ServiceStaff))).all()) == 1
        assert len((await session.scalars(select(DirectoryMutation))).all()) == 3
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, b.tenant_id)
        assert await session.get(BusinessService, service.id) is None


async def test_directory_cursor_filters_authorization_and_revision_binding(
    tenancy: TenancyFixture,
) -> None:
    a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
    directory = BusinessDirectoryService(tenancy.database.session_factory)
    records = [ServiceDefinition(id=uuid4(), name=f"Offering {index:02d}") for index in range(31)]
    await directory.get_directory(a)
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, a.tenant_id)
        draft = await session.scalar(
            select(ConfigurationDraft)
            .where(ConfigurationDraft.tenant_id == a.tenant_id)
            .with_for_update()
        )
        assert draft is not None
        document = dict(draft.document)
        document["directory"] = DirectorySnapshot(services=records).model_dump(mode="json")
        draft.document = document
        await sync_directory(session, a.tenant_id, DirectorySnapshot(services=records))
    cipher = SecretCipher(
        SecretStr(json.dumps({"test": base64.b64encode(bytes(range(32))).decode()})), "test"
    )
    first = await directory.list_records(a, BusinessListQuery(), cipher)
    assert len(first.items) == 25 and first.next_cursor
    second = await directory.list_records(a, BusinessListQuery(cursor=first.next_cursor), cipher)
    assert len(second.items) == 6 and second.next_cursor is None
    assert {item.record.id for item in first.items}.isdisjoint(
        {item.record.id for item in second.items}
    )
    for query in (
        BusinessListQuery(cursor=first.next_cursor, search="Offering"),
        BusinessListQuery(cursor=first.next_cursor + "!"),
    ):
        with pytest.raises(ConflictError):
            await directory.list_records(a, query, cipher)
    with pytest.raises(ConflictError):
        await directory.list_records(b, BusinessListQuery(cursor=first.next_cursor), cipher)
    assert (
        len(
            (await directory.list_records(a, BusinessListQuery(search="Offering 30"), cipher)).items
        )
        == 1
    )
    assert not (await directory.list_records(a, BusinessListQuery(search="%"), cipher)).items
    await directory.write(
        a,
        DirectoryWrite(
            request_id=uuid4(),
            draft_revision=first.draft_revision,
            expected_revision=1,
            record=records[0].model_copy(update={"name": "Updated offering"}),
        ),
    )
    with pytest.raises(RevisionConflictError):
        await directory.list_records(a, BusinessListQuery(cursor=first.next_cursor), cipher)


async def test_call_directory_reads_pinned_facts_without_private_staff_contacts(
    tenancy: TenancyFixture,
) -> None:
    context = await authenticate(tenancy)
    directory = BusinessDirectoryService(tenancy.database.session_factory)
    configuration = ConfigurationService(tenancy.database.session_factory)
    view = await directory.get_directory(context)
    staff = StaffDefinition(
        id=uuid4(),
        name="Published staff",
        job_title="Receptionist",
        email="private@example.test",
        phone="+919876543210",
    )
    saved = await directory.write(
        context,
        DirectoryWrite(
            request_id=uuid4(),
            draft_revision=view.draft_revision,
            expected_revision=0,
            record=staff,
        ),
    )
    service = ServiceDefinition(id=uuid4(), name="Published offering", price_minor=10000)
    saved = await directory.write(
        context,
        DirectoryWrite(
            request_id=uuid4(),
            draft_revision=saved.draft_revision,
            expected_revision=0,
            record=service,
        ),
    )
    published = await configuration.publish(context, saved.draft_revision)
    phone, call = uuid4(), uuid4()
    sid = "CA" + uuid4().hex
    async with tenancy.admin.begin() as session:
        session.add(
            PhoneNumber(
                id=phone,
                tenant_id=context.tenant_id,
                number="+" + str(int(uuid4().hex[:11], 16)),
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            Call(
                id=call,
                tenant_id=context.tenant_id,
                phone_number_id=phone,
                provider_call_id=sid,
                configuration_version=published.version,
                status="active",
            )
        )
    runtime = CallDirectoryService(tenancy.database.session_factory)
    bound = CallContext(context.tenant_id, call, sid, uuid4())
    facts = await runtime.business_read(bound, "staff", DirectoryQuery())
    assert (
        facts.items[0]["name"] == staff.name
        and "email" not in facts.items[0]
        and "phone" not in facts.items[0]
    )
    saved = await directory.write(
        context,
        DirectoryWrite(
            request_id=uuid4(),
            draft_revision=saved.draft_revision,
            expected_revision=1,
            record=service.model_copy(update={"price_minor": 90000}),
        ),
    )
    await configuration.publish(context, saved.draft_revision)
    assert (await runtime.business_read(bound, "services", DirectoryQuery())).items[0][
        "price_minor"
    ] == "10000"


async def test_business_list_http_query_validation_and_boundaries(tenancy: TenancyFixture) -> None:
    settings = tenancy.settings.model_copy(
        update={
            "secret_encryption_keys": SecretStr(
                json.dumps({"test": base64.b64encode(bytes(range(32))).decode()})
            ),
            "secret_encryption_active_key": "test",
        }
    )
    app = create_app(settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"},
        ) as client,
    ):
        for query in ("", "?limit=25", "?limit=50&active=false", "?search=hello&limit=100"):
            response = await client.get("/api/v2/business/records" + query)
            assert response.status_code == 200
            assert response.json()["items"] == [] and response.json()["next_cursor"] is None
        for query in ("?limit=26", "?kind=unknown", "?active=unknown", "?foreign=field"):
            assert (await client.get("/api/v2/business/records" + query)).status_code == 422


async def test_rollback_restores_swapped_names_and_keeps_removed_entity_history(
    tenancy: TenancyFixture,
) -> None:
    context = await authenticate(tenancy)
    directory = BusinessDirectoryService(tenancy.database.session_factory)
    configuration = ConfigurationService(tenancy.database.session_factory)
    view = await directory.get_directory(context)
    a, b = (
        ServiceDefinition(id=uuid4(), name="Offering A"),
        ServiceDefinition(id=uuid4(), name="Offering B"),
    )
    first = await directory.write(
        context,
        DirectoryWrite(
            request_id=uuid4(), draft_revision=view.draft_revision, expected_revision=0, record=a
        ),
    )
    second = await directory.write(
        context,
        DirectoryWrite(
            request_id=uuid4(), draft_revision=first.draft_revision, expected_revision=0, record=b
        ),
    )
    published = await configuration.publish(context, second.draft_revision)
    revision = second.draft_revision
    for record, expected in (
        (a.model_copy(update={"name": "Temporary"}), 1),
        (b.model_copy(update={"name": "Offering A"}), 1),
        (a.model_copy(update={"name": "Offering B"}), 2),
    ):
        result = await directory.write(
            context,
            DirectoryWrite(
                request_id=uuid4(),
                draft_revision=revision,
                expected_revision=expected,
                record=record,
            ),
        )
        revision = result.draft_revision
    extra = StaffDefinition(id=uuid4(), name="Later staff")
    result = await directory.write(
        context,
        DirectoryWrite(
            request_id=uuid4(), draft_revision=revision, expected_revision=0, record=extra
        ),
    )
    restored = await configuration.rollback(context, published.version, result.draft_revision)
    assert {row.id: row.name for row in restored.document.directory.services} == {
        a.id: a.name,
        b.id: b.name,
    }
    assert not restored.document.directory.staff
    assert not (await directory.directory_catalog(context))["staff"]
