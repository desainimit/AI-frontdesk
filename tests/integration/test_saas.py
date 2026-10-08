"""Exercise publication and dynamic authorization with the restricted PostgreSQL role."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.auth.context import RequestContext
from app.auth.human_schemas import InviteMember
from app.auth.policies import Permission, require_permission
from app.auth.service import AuthenticationService
from app.configuration.models import ConfigurationEvent, PublishedConfiguration
from app.configuration.schemas import ConfigurationTest, SaveConfiguration
from app.configuration.service import ConfigurationService, is_business_open, published_for_tenant
from app.conversation.locales import LanguagePolicy
from app.database.rls import set_tenant_scope
from app.database.unit_of_work import UnitOfWork
from app.errors import AuthorizationError, ConflictError, NotFoundError
from app.platform.support import EnterSupportCommand, SupportAccessService
from app.rbac.models import TenantRole
from app.rbac.schemas import PermissionGrant, RoleCommand, UpdateRole
from app.rbac.service import RevisionConflictError, RoleService
from app.tenants.readiness import NotReadyError, TenantReadinessService
from app.tenants.schemas import TenantName, TransferDestination
from app.tenants.service import TenantService
from app.users.models import User
from tests.integration.test_human_auth import Harness
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as tenancy_marks
from tests.integration.test_tenancy import tenancy as tenancy_fixture

tenancy = tenancy_fixture
pytestmark = tenancy_marks


async def authenticate(fixture: TenancyFixture, *, second: bool = False) -> RequestContext:
    account = fixture.b if second else fixture.a
    return await AuthenticationService(fixture.database.session_factory).authenticate(
        account.token.get_secret_value(), uuid4()
    )


async def test_independent_drafts_publication_history_and_rollback(tenancy: TenancyFixture) -> None:
    a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
    service = ConfigurationService(tenancy.database.session_factory)
    first_a, first_b = await service.get(a), await service.get(b)
    for context, draft, greeting in ((a, first_a, "Welcome to A"), (b, first_b, "Bonjour chez B")):
        document = draft.document.model_copy(deep=True)
        document.agent.greeting = greeting
        saved = await service.save(
            context, SaveConfiguration(revision=draft.revision, document=document)
        )
        assert saved.revision == 2
        published = await service.publish(context, saved.revision)
        assert published.version == 1
        assert (await service.publish(context, saved.revision)).version == 1
        assert (await service.effective(context)).document.agent.greeting == greeting
    edited = (await service.get(a)).document.model_copy(deep=True)
    edited.agent.greeting = "An unpublished change"
    saved_a = await service.save(a, SaveConfiguration(revision=2, document=edited))
    for context, expected in ((a, "Welcome to A"), (b, "Bonjour chez B")):
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, context.tenant_id)
            runtime = await published_for_tenant(session, context.tenant_id)
            assert runtime is not None and runtime[1].agent.greeting == expected
            assert len((await session.scalars(select(PublishedConfiguration))).all()) == 1
            assert len((await session.scalars(select(ConfigurationEvent))).all()) == 1
    second = await service.publish(a, saved_a.revision)
    assert second.version == 2
    restored = await service.rollback(a, 1, saved_a.revision)
    assert restored.document.agent.greeting == "Welcome to A"
    assert (await service.history(a))[0].document.agent.greeting == "An unpublished change"
    third = await service.publish(a, restored.revision)
    assert third.version == 3 and third.content_hash == (await service.history(a))[-1].content_hash
    assert (await service.get(b)).published_version == 1


async def test_concurrent_edit_has_one_winner(tenancy: TenancyFixture) -> None:
    context = await authenticate(tenancy)
    service = ConfigurationService(tenancy.database.session_factory)
    draft = await service.get(context)
    results = await asyncio.gather(
        *(
            service.save(
                context, SaveConfiguration(revision=draft.revision, document=draft.document)
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, RevisionConflictError) for result in results) == 1
    assert (await service.get(context)).revision == 2


async def test_published_configuration_cannot_be_bypassed_by_legacy_writes(
    tenancy: TenancyFixture,
) -> None:
    context = await authenticate(tenancy)
    configuration = ConfigurationService(tenancy.database.session_factory)
    draft = await configuration.get(context)
    await configuration.publish(context, draft.revision)
    legacy = TenantService(UnitOfWork(tenancy.database.session_factory))
    for command in (
        legacy.rename(context, TenantName(name="Bypass")),
        legacy.set_language_policy(context, LanguagePolicy(default_language="fr")),
        legacy.set_transfer_destination(context, TransferDestination(number="+919876543210")),
    ):
        with pytest.raises(ConflictError):
            await command
    assert (await configuration.effective(context)).document == draft.document


async def test_dry_run_and_unavailable_workflows_cannot_activate(tenancy: TenancyFixture) -> None:
    context = await authenticate(tenancy)
    service = ConfigurationService(tenancy.database.session_factory)
    draft = await service.get(context)
    result = await service.test(
        context,
        ConfigurationTest(revision=draft.revision, instant=datetime(2026, 10, 8, tzinfo=UTC)),
    )
    assert result.external_actions_executed is False
    assert result.greeting == draft.document.agent.greeting
    document = draft.document.model_copy(deep=True)
    document.workflows.consented_callbacks = True
    saved = await service.save(context, SaveConfiguration(revision=1, document=document))
    validation = await service.validate(context, saved.revision)
    assert any(issue.field == "workflows.consented_callbacks" for issue in validation.issues)
    with pytest.raises(ConflictError):
        await service.publish(context, saved.revision)


async def test_validation_and_immutable_published_records(tenancy: TenancyFixture) -> None:
    context = await authenticate(tenancy)
    service = ConfigurationService(tenancy.database.session_factory)
    draft = await service.get(context)
    invalid = draft.document.model_copy(deep=True)
    invalid.voice.enabled = True
    invalid.voice.primary_connection_id = uuid4()
    saved = await service.save(context, SaveConfiguration(revision=1, document=invalid))
    validation = await service.validate(context, saved.revision)
    assert not validation.valid
    assert {issue.code for issue in validation.issues} >= {"required", "connection_unavailable"}
    with pytest.raises(ConflictError):
        await service.publish(context, saved.revision)
    assert await service.history(context) == []
    valid = await service.save(context, SaveConfiguration(revision=2, document=draft.document))
    await service.publish(context, valid.revision)
    with pytest.raises(DBAPIError):
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, context.tenant_id)
            await session.execute(update(PublishedConfiguration).values(content_hash="0" * 64))


async def test_role_names_do_not_grant_access_and_revocation_is_immediate(
    tenancy: TenancyFixture,
) -> None:
    context = await authenticate(tenancy)
    roles = RoleService(tenancy.database.session_factory)
    restricted = await roles.save(
        context,
        RoleCommand(
            name="Owner renamed without privileges",
            permissions=[
                PermissionGrant(module="tenant", permission="read", allowed=True),
            ],
        ),
    )
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, context.tenant_id)
        other = User(
            tenant_id=context.tenant_id,
            email="other@example.test",
            display_name="Other",
            role="admin",
            role_id=context.role_id,
        )
        session.add(other)
        await session.flush()
    assert context.actor_id is not None
    await roles.assign(context, context.actor_id, restricted.id)
    with pytest.raises(AuthorizationError):
        await ConfigurationService(tenancy.database.session_factory).get(context)
    current = await authenticate(tenancy)
    assert current.role_name == restricted.name
    require_permission(current, Permission.TENANT_READ)
    with pytest.raises(AuthorizationError):
        require_permission(current, Permission.CONFIGURATION_READ)
    with pytest.raises(AuthorizationError):
        await roles.save(
            current,
            RoleCommand(
                name="Escalation",
                permissions=[
                    PermissionGrant(module="tenant", permission="read", allowed=True),
                    PermissionGrant(module="role", permission="read", allowed=True),
                ],
            ),
        )


async def test_last_custodian_and_cross_tenant_role_binding(tenancy: TenancyFixture) -> None:
    a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
    roles = RoleService(tenancy.database.session_factory)
    owner = next(role for role in await roles.list(a) if role.id == a.role_id)
    with pytest.raises(ConflictError):
        await roles.save(
            a, UpdateRole(name=owner.name, revision=owner.revision, permissions=[]), owner.id
        )
    assert (next(role for role in await roles.list(a) if role.id == owner.id)).revision == 1
    with pytest.raises(ConflictError):
        await roles.delete(a, owner.id, owner.revision)
    assert b.role_id is not None and a.actor_id is not None
    with pytest.raises(NotFoundError):
        await roles.assign(a, a.actor_id, b.role_id)
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, a.tenant_id)
        assert all(
            role.tenant_id == a.tenant_id for role in await session.scalars(select(TenantRole))
        )


def test_business_hours_are_timezone_aware_and_holidays_take_precedence() -> None:
    from app.configuration.schemas import OperationalConfiguration

    document = OperationalConfiguration.model_validate(
        {
            "hours": {
                "windows": [{"weekday": 0, "opens": "09:00", "closes": "17:00"}],
                "holidays": ["2026-10-12"],
            }
        }
    )
    assert is_business_open(document, datetime(2026, 10, 5, 4, tzinfo=UTC))
    assert not is_business_open(document, datetime(2026, 10, 5, 12, tzinfo=UTC))
    assert not is_business_open(document, datetime(2026, 10, 12, 4, tzinfo=UTC))


async def test_pending_owner_configuration_and_deleted_roles_survive_invitations(
    tenancy: TenancyFixture,
) -> None:
    harness = Harness(tenancy)
    platform = await harness.platform()
    tenant = await harness.onboard(platform, f"pending-{uuid4()}@example.test")
    support = SupportAccessService(tenancy.database.session_factory)
    entered = await support.enter(
        platform, EnterSupportCommand(tenant_id=tenant.tenant_id, reason="Configure pending owner")
    )
    context = await support.authenticate(entered.access_token, uuid4())
    roles = RoleService(tenancy.database.session_factory)
    values = await roles.list(context)
    owner = next(role for role in values if role.name == "Owner")
    updated = await roles.save(
        context,
        UpdateRole(name="Business owner", revision=owner.revision, permissions=owner.permissions),
        owner.id,
    )
    assert updated.revision == 2
    analyst = next(role for role in values if role.name == "Analyst")
    await roles.delete(context, analyst.id, analyst.revision)
    await harness.auth.invite_member(
        context,
        InviteMember(
            email=f"invited-{uuid4()}@example.test",
            display_name="Invited owner",
            role_id=owner.id,
        ),
        str(uuid4()),
    )
    assert analyst.id not in {role.id for role in await roles.list(context)}
    configuration = ConfigurationService(tenancy.database.session_factory)
    draft = await configuration.get(context)
    await configuration.publish(context, draft.revision)
    readiness = TenantReadinessService(tenancy.database.session_factory, harness.settings)
    checks = (await readiness.readiness(context)).checks
    assert (
        next(check for check in checks if check.key == "published_configuration").status == "ready"
    )
    assert (
        next(check for check in checks if check.key == "accepted_administrator").status
        == "blocking"
    )
    # Billing is not part of readiness: nothing but the real checks can block going live.
    assert all(check.key != "billing_activation" for check in checks)
    with pytest.raises(NotReadyError):
        await readiness.set_live(context, True)
