"""Module-owned scope writes are bounded, atomic and isolated under forced RLS."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.configuration.resources import ConfigurationResourceService, CreateConfigurationResource
from app.configuration.schemas import SaveConfigurationScopes, ScopeChange
from app.configuration.sections import (
    InvalidConfigurationSectionError,
    SectionPublicationConflictError,
)
from app.configuration.service import ConfigurationService
from app.errors import AuthorizationError, NotFoundError
from app.rbac.service import RevisionConflictError
from tests.integration.test_configuration_sections import editor
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_module_owned_scope_commands_are_atomic_and_revision_checked(
    tenancy: TenancyFixture,
) -> None:
    owner, actor = await authenticate(tenancy), await editor(tenancy)
    service = ConfigurationService(tenancy.database.session_factory)
    resources = ConfigurationResourceService(tenancy.database.session_factory)
    agent = await resources.create(
        actor, CreateConfigurationResource(id=uuid4(), kind="agent", name="Reception")
    )
    with pytest.raises(AuthorizationError):
        await resources.create(
            actor,
            CreateConfigurationResource(
                id=uuid4(), kind="channel", channel_kind="voice", name="Private channel"
            ),
        )
    initial = await service.get(owner)
    command = SaveConfigurationScopes(
        revision=initial.revision,
        changes=[ScopeChange(kind="agent", id=agent.id, value={"greeting": "Scoped greeting"})],
    )
    saved = await service.save_scopes(actor, command)
    assert saved.revision == initial.revision + 1
    assert (
        isinstance(saved.changes[0].value, dict)
        and saved.changes[0].value["greeting"] == "Scoped greeting"
    )
    own = await service.get_scope(actor, "agent", agent.id)
    assert own.value == saved.changes[0].value and own.section == "agent"
    assert set(own.inherited_values) == set((await service.catalog(actor))["scope_fields"]["agent"])  # type: ignore[index]
    assert "privacy" not in own.inherited_values
    with pytest.raises(RevisionConflictError):
        await service.save_scopes(actor, command)
    with pytest.raises(InvalidConfigurationSectionError):
        await service.save_scopes(
            actor,
            SaveConfigurationScopes(
                revision=saved.revision,
                changes=[ScopeChange(kind="agent", id=agent.id, value={"unimplemented": True})],
            ),
        )
    channel = await resources.create(
        owner,
        CreateConfigurationResource(id=uuid4(), kind="channel", channel_kind="chat", name="Chat"),
    )
    with pytest.raises(AuthorizationError):
        await service.get_scope(actor, "channel", channel.id)
    with pytest.raises(AuthorizationError):
        await service.save_scopes(
            actor,
            SaveConfigurationScopes(
                revision=saved.revision,
                changes=[
                    ScopeChange(kind="agent", id=agent.id, value={"greeting": "Must roll back"}),
                    ScopeChange(kind="channel", id=channel.id, value={"kind": "chat"}),
                ],
            ),
        )
    foreign = await resources.create(
        await authenticate(tenancy, second=True),
        CreateConfigurationResource(id=uuid4(), kind="agent", name="Foreign"),
    )
    with pytest.raises(NotFoundError):
        await service.save_scopes(
            actor,
            SaveConfigurationScopes(
                revision=saved.revision,
                changes=[
                    ScopeChange(kind="agent", id=foreign.id, value={"greeting": "Cross tenant"})
                ],
            ),
        )
    with pytest.raises(NotFoundError):
        await service.get_scope(actor, "agent", foreign.id)
    current = await service.get(owner)
    assert (
        current.revision == saved.revision
        and current.document.scopes.agents[agent.id].greeting == "Scoped greeting"
    )
    inherited = await service.save_scopes(
        actor,
        SaveConfigurationScopes(
            revision=saved.revision, changes=[ScopeChange(kind="agent", id=agent.id, value=None)]
        ),
    )
    assert (
        inherited.changes[0].value is None
        and agent.id not in (await service.get(owner)).document.scopes.agents
    )


async def test_module_publication_includes_owned_scopes_only(tenancy: TenancyFixture) -> None:
    owner, actor = await authenticate(tenancy), await editor(tenancy)
    service = ConfigurationService(tenancy.database.session_factory)
    resources = ConfigurationResourceService(tenancy.database.session_factory)
    baseline = await service.get(owner)
    await service.publish(owner, baseline.revision)
    agent = await resources.create(
        actor, CreateConfigurationResource(id=uuid4(), kind="agent", name="Reception")
    )
    saved = await service.save_scopes(
        actor,
        SaveConfigurationScopes(
            revision=baseline.revision,
            changes=[ScopeChange(kind="agent", id=agent.id, value={"greeting": "Published scope"})],
        ),
    )
    review = await service.section_diff(actor, "agent", saved.revision)
    assert review.validation.valid and any(
        change.field.startswith("scopes.agents.") for change in review.changes
    )
    published = await service.publish(actor, saved.revision, section="agent")
    assert published.document.scopes.agents[agent.id].greeting == "Published scope"
    view = await service.get_scope(actor, "agent", agent.id)
    assert view.effective_values["agent.greeting"] == "Published scope"
    assert view.field_origins["agent.greeting"] == f"agent:{agent.id}"
    assert view.inherited_values["agent.greeting"] != "Published scope"
    channel = await resources.create(
        owner,
        CreateConfigurationResource(
            id=uuid4(), kind="channel", channel_kind="voice", name="Telephone"
        ),
    )
    dirty = await service.save_scopes(
        owner,
        SaveConfigurationScopes(
            revision=saved.revision,
            changes=[
                ScopeChange(
                    kind="channel",
                    id=channel.id,
                    value={"kind": "voice", "greeting": "Outside agent authority"},
                )
            ],
        ),
    )
    with pytest.raises(SectionPublicationConflictError):
        await service.publish(actor, dirty.revision, section="agent")
    review = await service.section_diff(actor, "agent", dirty.revision)
    assert not review.validation.valid and all(
        not change.field.startswith("scopes.channels") for change in review.changes
    )
    channel_view = await service.get_scope(owner, "channel", channel.id)
    assert channel_view.inherited_values["agent.greeting"] == baseline.document.agent.greeting


def test_scope_batch_rejects_duplicates_and_excessive_commands() -> None:
    change = ScopeChange(kind="agent", id=uuid4(), value=None)
    with pytest.raises(ValidationError):
        SaveConfigurationScopes(revision=1, changes=[change, change])
    with pytest.raises(ValidationError):
        SaveConfigurationScopes(
            revision=1,
            changes=[ScopeChange(kind="agent", id=uuid4(), value=None) for _ in range(26)],
        )
