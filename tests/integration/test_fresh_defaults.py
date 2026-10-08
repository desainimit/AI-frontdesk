"""A new SaaS workspace is fully initialized before any settings page is read."""

from datetime import time
from uuid import uuid4

from sqlalchemy import func, select

from app.agents.catalog import MUTATION_TOOLS, SAFE_READ_TOOLS
from app.auth.models import ApiCredential
from app.auth.policies import Permission
from app.configuration.defaults import DEFAULTS_VERSION, tenant_defaults
from app.configuration.hours import HoursPolicy
from app.configuration.models import ConfigurationDraft, PublishedConfiguration
from app.configuration.schemas import OperationalConfiguration, SaveConfiguration
from app.configuration.service import ConfigurationService, seed_configuration
from app.database.rls import set_tenant_scope
from app.onboarding.models import SetupProgress
from app.platform.onboarding import TenantOnboardingService, onboarding_defaults
from app.platform.support import EnterSupportCommand, SupportAccessService
from app.rbac.catalog import template_permissions
from app.rbac.models import RolePermission, TenantRole
from app.rbac.service import seed_roles
from app.tenants.models import Tenant
from app.users.models import User
from tests.integration.test_billing_accounts import platform
from tests.integration.test_onboarding import draft
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_new_workspace_defaults_are_persisted_safe_and_idempotent(
    tenancy: TenancyFixture,
) -> None:
    actor = await platform(tenancy)
    service = TenantOnboardingService(tenancy.database.session_factory)
    command = draft()
    defaults = onboarding_defaults()
    command.language_policy = defaults.language_policy
    key = str(uuid4())
    created = await service.create(actor, command, key)
    assert await service.create(actor, command, key) == created
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, created.tenant_id)
        tenant = await session.get(Tenant, created.tenant_id)
        assert tenant and tenant.requires_saas_activation and tenant.service_state == "setup"
        row = await session.scalar(select(ConfigurationDraft))
        assert row and row.defaults_version == DEFAULTS_VERSION and row.revision == 1
        original = row.document.copy()
        config = OperationalConfiguration.model_validate(row.document)
        assert config.business.name == command.name and config.business.timezone == "Asia/Kolkata"
        assert config.languages.enabled_languages == ["en"]
        assert set(config.agent.allowed_tools) == SAFE_READ_TOOLS
        assert not set(config.agent.allowed_tools) & MUTATION_TOOLS
        assert config.voice.max_duration_minutes == 20 and not config.voice.enabled
        assert config.workflows.quiet_hours_start == time(20)
        assert config.workflows.quiet_hours_end == time(8)
        assert config.workflows.maximum_attempts_per_day == 3
        assert config.privacy.transcript_retention_days == 90
        assert (
            config.privacy.recording_retention_days == 30 and not config.privacy.recording_enabled
        )
        assert not config.budget.auto_recharge and config.budget.monthly_budget_paisa == 0
        assert not config.messaging.sms_enabled and not config.messaging.email_enabled
        assert not config.chat.enabled and not config.scheduling.enabled
        assert config.voice.primary_connection_id is None
        assert config.hours.windows == []  # No invented opening hours for a real business.
        assert await session.scalar(select(func.count()).select_from(HoursPolicy)) == 1
        progress = await session.scalar(select(SetupProgress))
        assert progress and progress.current_step == "business" and progress.reviewed == {}
        assert progress.revision == 1
        assert await session.scalar(select(func.count()).select_from(PublishedConfiguration)) == 0
        assert await session.scalar(select(func.count()).select_from(ApiCredential)) == 0
        member = await session.get(User, created.admin_id)
        assert member and not member.is_active
        roles = list(await session.scalars(select(TenantRole)))
        assert len(roles) == 6
        for role in roles:
            grants = list(
                await session.scalars(
                    select(RolePermission).where(RolePermission.role_id == role.id)
                )
            )
            assert {grant.key for grant in grants} == {
                permission.value for permission in Permission
            }
            assert {grant.key for grant in grants if grant.allowed} == template_permissions(
                role.template_key or ""
            )
        owner = next(role for role in roles if role.template_key == "owner")
        assert member.role_id == owner.id
        # Re-running the helpers does not add rows or reset any tenant values.
        assert (await seed_configuration(session, tenant)).document == original
        assert len(await seed_roles(session, tenant.id)) == 6


async def test_new_defaults_never_replace_saved_tenant_configuration(
    tenancy: TenancyFixture,
) -> None:
    actor = await platform(tenancy)
    onboarding = TenantOnboardingService(tenancy.database.session_factory)
    created = await onboarding.create(actor, draft(), str(uuid4()))
    support = SupportAccessService(tenancy.database.session_factory)
    entered = await support.enter(
        actor,
        EnterSupportCommand(
            tenant_id=created.tenant_id, reason="Verify fresh workspace editable defaults"
        ),
    )
    context = await support.authenticate(entered.access_token, uuid4())
    configuration = ConfigurationService(tenancy.database.session_factory)
    first = await configuration.get(context)
    document = first.document.model_copy(deep=True)
    document.agent.greeting = "Our workspace greeting survives initialization."
    document.business.timezone = "Europe/Paris"
    saved = await configuration.save(
        context, SaveConfiguration(revision=first.revision, document=document)
    )
    seed = tenant_defaults()
    seed.agent.greeting = "Mutating one default object must not affect another."
    assert tenant_defaults().agent.greeting != seed.agent.greeting
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, created.tenant_id)
        tenant = await session.get(Tenant, created.tenant_id)
        assert tenant is not None
        unchanged = await seed_configuration(session, tenant)
        assert unchanged.document == saved.document.model_dump(mode="json")
        assert unchanged.revision == saved.revision
    assert (
        await ConfigurationService(tenancy.database.session_factory).get(context)
    ).document == saved.document
