"""Scope precedence, tenant ownership, atomic publication and pinned runtime behavior."""

from datetime import time
from uuid import uuid4

import pytest

from app.calls.models import Call, PhoneNumber
from app.calls.service import CallContext, CallService
from app.configuration.resources import (
    ConfigurationResourceService,
    CreateConfigurationResource,
    UpdateConfigurationResource,
)
from app.configuration.schemas import (
    AgentOverride,
    ChannelOverride,
    HoursSettings,
    LocationOverride,
    OpeningWindow,
    SaveConfiguration,
)
from app.configuration.service import ConfigurationService
from app.errors import AuthorizationError, ConflictError, NotFoundError
from app.locations.models import Location
from app.rbac.service import RevisionConflictError
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_resources_idempotency_revision_and_cross_tenant_rls(tenancy: TenancyFixture) -> None:
    a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
    service = ConfigurationResourceService(tenancy.database.session_factory)
    command = CreateConfigurationResource(id=uuid4(), kind="agent", name="Reception")
    first = await service.create(a, command)
    assert (await service.create(a, command)).id == first.id
    with pytest.raises(ConflictError):
        await service.create(a, command.model_copy(update={"name": "Changed request"}))
    with pytest.raises(NotFoundError):
        await service.update(
            b,
            first.id,
            UpdateConfigurationResource(revision=1, name="Other tenant", is_active=False),
        )
    renamed = await service.update(
        a, first.id, UpdateConfigurationResource(revision=1, name="Reception agent", is_active=True)
    )
    assert renamed.revision == 2 and (await service.create(a, command)).name == renamed.name
    with pytest.raises(RevisionConflictError):
        await service.update(
            a, first.id, UpdateConfigurationResource(revision=1, name="Stale", is_active=True)
        )
    assert not (await service.catalog(b))["scope_resources"]


async def test_scope_precedence_inheritance_and_pinned_calls(tenancy: TenancyFixture) -> None:
    context = await authenticate(tenancy)
    resources = ConfigurationResourceService(tenancy.database.session_factory)
    agent = await resources.create(
        context, CreateConfigurationResource(id=uuid4(), kind="agent", name="Reception")
    )
    channel = await resources.create(
        context,
        CreateConfigurationResource(
            id=uuid4(), kind="channel", channel_kind="voice", name="Main number"
        ),
    )
    configuration = ConfigurationService(tenancy.database.session_factory)
    location_id, foreign_location_id = uuid4(), uuid4()
    async with tenancy.admin.begin() as session:
        session.add_all(
            [
                Location(
                    id=location_id,
                    tenant_id=tenancy.a.tenant_id,
                    name="Main office",
                    timezone="Asia/Kolkata",
                ),
                Location(
                    id=foreign_location_id,
                    tenant_id=tenancy.b.tenant_id,
                    name="Other office",
                    timezone="Asia/Kolkata",
                ),
            ]
        )
    draft = await configuration.get(context)
    document = draft.document.model_copy(deep=True)
    document.scopes.locations[location_id] = LocationOverride(
        timezone="Europe/Paris",
        address="Paris office",
        hours=HoursSettings(windows=[OpeningWindow(weekday=0, opens=time(9), closes=time(17))]),
    )
    document.scopes.agents[agent.id] = AgentOverride(
        location_id=location_id, greeting="Agent greeting"
    )
    document.scopes.channels[channel.id] = ChannelOverride(
        kind="voice",
        agent_id=agent.id,
        greeting="Channel greeting",
        max_duration_minutes=12,
        transfer_number="",
    )
    saved = await configuration.save(
        context, SaveConfiguration(revision=draft.revision, document=document)
    )
    assert (await configuration.validate(context, saved.revision)).valid
    assert await configuration.history(context) == []
    first = await configuration.publish(context, saved.revision)
    effective = await configuration.effective(context, channel_id=channel.id)
    assert effective.document.agent.greeting == "Channel greeting"
    assert effective.document.business.timezone == "Europe/Paris"
    assert effective.document.voice.max_duration_minutes == 12
    assert effective.field_origins["agent.greeting"] == f"channel:{channel.id}"
    assert effective.field_origins["business.timezone"] == f"location:{location_id}"
    assert effective.field_origins["agent.tone"] == "tenant"
    with pytest.raises(NotFoundError):
        await configuration.effective(
            context, channel_id=channel.id, location_id=foreign_location_id
        )
    phone, call = uuid4(), uuid4()
    sid = "CA" + uuid4().hex
    async with tenancy.admin.begin() as session:
        session.add(
            PhoneNumber(
                id=phone,
                tenant_id=context.tenant_id,
                number="+91" + str(int(uuid4().hex[:9], 16)).zfill(11),
            )
        )
        await session.flush()
        session.add(
            Call(
                id=call,
                tenant_id=context.tenant_id,
                phone_number_id=phone,
                provider_call_id=sid,
                configuration_version=first.version,
                location_id=location_id,
                agent_id=agent.id,
                channel_id=channel.id,
            )
        )
    bound = CallContext(context.tenant_id, call, sid, uuid4())
    runtime = CallService(tenancy.database.session_factory)
    pinned = await runtime.operational_configuration(bound)
    assert pinned is not None and pinned[1].agent.greeting == "Channel greeting"
    inherited = document.model_copy(deep=True)
    inherited.scopes.channels[channel.id].greeting = None
    second = await configuration.save(
        context, SaveConfiguration(revision=saved.revision, document=inherited)
    )
    await configuration.publish(context, second.revision)
    assert (
        await configuration.effective(context, channel_id=channel.id)
    ).document.agent.greeting == "Agent greeting"
    pinned = await runtime.operational_configuration(bound)
    assert pinned is not None and pinned[1].agent.greeting == "Channel greeting"
    await resources.update(
        context,
        channel.id,
        UpdateConfigurationResource(revision=1, name=channel.name, is_active=False),
    )
    with pytest.raises(AuthorizationError):
        await runtime.start(bound, "MZ" + uuid4().hex)


async def test_scope_publish_rejects_foreign_references_and_unsupported_activation(
    tenancy: TenancyFixture,
) -> None:
    a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
    resources = ConfigurationResourceService(tenancy.database.session_factory)
    foreign = await resources.create(
        b, CreateConfigurationResource(id=uuid4(), kind="agent", name="Private agent")
    )
    chat = await resources.create(
        a,
        CreateConfigurationResource(
            id=uuid4(), kind="channel", channel_kind="chat", name="Website"
        ),
    )
    config = ConfigurationService(tenancy.database.session_factory)
    draft = await config.get(a)
    document = draft.document.model_copy(deep=True)
    document.scopes.agents[foreign.id] = AgentOverride(greeting="Foreign")
    saved = await config.save(a, SaveConfiguration(revision=draft.revision, document=document))
    assert not (await config.validate(a, saved.revision)).valid
    with pytest.raises(ConflictError):
        await config.publish(a, saved.revision)
    document.scopes.agents.clear()
    from app.configuration.schemas import ChatSettings

    document.scopes.channels[chat.id] = ChannelOverride(
        kind="chat", chat=ChatSettings(enabled=True, allowed_origins=["https://example.test"])
    )
    saved = await config.save(a, SaveConfiguration(revision=saved.revision, document=document))
    assert not (await config.validate(a, saved.revision)).valid
    with pytest.raises(ConflictError):
        await config.publish(a, saved.revision)
