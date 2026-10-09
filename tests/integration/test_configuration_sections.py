"""Section grants cannot read, replace or publish unrelated tenant configuration."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.auth.context import ActorType, RequestContext, Role
from app.configuration.schemas import SaveConfiguration, SaveConfigurationSection
from app.configuration.sections import (
    InitialPublicationRequiredError,
    InvalidConfigurationSectionError,
    SectionPublicationConflictError,
)
from app.configuration.service import ConfigurationService
from app.core.database.rls import set_tenant_scope
from app.core.errors import AuthorizationError
from app.main import create_app
from app.rbac.schemas import PermissionGrant, RoleCommand
from app.rbac.service import RevisionConflictError, RoleService, bind_user_permissions
from app.users.models import User
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def editor(tenancy: TenancyFixture) -> RequestContext:
    owner = await authenticate(tenancy)
    role = await RoleService(tenancy.database.session_factory).save(
        owner,
        RoleCommand(
            name="Agent editor",
            permissions=[
                PermissionGrant(module=module, permission=action, allowed=True)
                for module, action in (
                    ("tenant", "read"),
                    ("agent", "read"),
                    ("agent", "update"),
                    ("agent", "publish"),
                )
            ],
        ),
    )
    user = User(
        id=uuid4(),
        tenant_id=owner.tenant_id,
        email=f"{uuid4()}@example.test",
        display_name="Agent editor",
        role="member",
        role_id=role.id,
        is_active=True,
    )
    async with tenancy.admin.begin() as session:
        session.add(user)
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, owner.tenant_id)
        return await bind_user_permissions(
            session,
            RequestContext(uuid4(), owner.tenant_id, user.id, ActorType.USER, Role.MEMBER),
            user,
        )


async def test_section_access_validation_revision_and_immediate_revocation(
    tenancy: TenancyFixture,
) -> None:
    service = ConfigurationService(tenancy.database.session_factory)
    context = await editor(tenancy)
    view = await service.get_section(context, "agent")
    assert isinstance(view.value, dict) and "greeting" in view.value
    assert "privacy" not in view.model_dump() and "document" not in view.model_dump()
    with pytest.raises(AuthorizationError):
        await service.get(context)
    with pytest.raises(AuthorizationError):
        await service.get_section(context, "privacy")
    with pytest.raises(AuthorizationError):
        await service.save_section(
            context, "privacy", SaveConfigurationSection(revision=view.revision, value={})
        )
    with pytest.raises(InvalidConfigurationSectionError):
        await service.save_section(
            context,
            "agent",
            SaveConfigurationSection(revision=view.revision, value={"unknown": "reject"}),
        )
    assert (await service.get_section(context, "agent")).revision == view.revision
    value = {**view.value, "greeting": "Agent configured independently"}
    saved = await service.save_section(
        context, "agent", SaveConfigurationSection(revision=view.revision, value=value)
    )
    with pytest.raises(RevisionConflictError):
        await service.save_section(
            context, "agent", SaveConfigurationSection(revision=view.revision, value=value)
        )
    assert saved.value == value
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(User).where(User.id == context.actor_id).values(is_active=False)
        )
    with pytest.raises(AuthorizationError):
        await service.save_section(
            context, "agent", SaveConfigurationSection(revision=saved.revision, value=value)
        )


async def test_section_publication_cannot_release_another_admins_changes(
    tenancy: TenancyFixture,
) -> None:
    owner = await authenticate(tenancy)
    context = await editor(tenancy)
    service = ConfigurationService(tenancy.database.session_factory)
    baseline = await service.get(owner)
    with pytest.raises(InitialPublicationRequiredError):
        await service.publish(context, baseline.revision, section="agent")
    await service.publish(owner, baseline.revision)
    view = await service.get_section(context, "agent")
    assert isinstance(view.value, dict)
    saved = await service.save_section(
        context,
        "agent",
        SaveConfigurationSection(
            revision=view.revision, value={**view.value, "greeting": "Only this tenant changed"}
        ),
    )
    result = await service.publish(context, saved.revision, section="agent")
    assert result.version == 2 and result.document.agent.greeting == "Only this tenant changed"
    current = await service.get(owner)
    document = current.document.model_copy(deep=True)
    document.privacy.transcript_retention_days = 60
    dirty = await service.save(
        owner, SaveConfiguration(revision=current.revision, document=document)
    )
    with pytest.raises(SectionPublicationConflictError):
        await service.publish(context, dirty.revision, section="agent")
    review = await service.section_diff(context, "agent", dirty.revision)
    assert not review.validation.valid
    assert all(not change.field.startswith("privacy.") for change in review.changes)
    assert [issue.code for issue in review.validation.issues] == ["section_publication_conflict"]
    assert (await service.get_section(context, "agent")).published_version == 2
    foreign = await service.get_section(await authenticate(tenancy, second=True), "agent")
    assert foreign.published_version is None and foreign.value != saved.value


async def test_section_http_returns_only_owned_values_and_publication_summary(
    tenancy: TenancyFixture,
) -> None:
    app = create_app(tenancy.settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
        response = await client.get("/api/v2/configuration/sections/agent", headers=headers)
        assert response.status_code == 200 and "document" not in response.json()
        revision = response.json()["revision"]
        initial = await client.post(
            "/api/v2/configuration/publish", headers=headers, json={"revision": revision}
        )
        assert initial.status_code == 200
        response = await client.post(
            "/api/v2/configuration/sections/agent/publish",
            headers=headers,
            json={"revision": revision},
        )
        assert response.status_code == 200 and set(response.json()) == {
            "version",
            "source_revision",
            "content_hash",
        }
        assert (
            await client.get("/api/v2/configuration/sections/directory", headers=headers)
        ).status_code == 422
