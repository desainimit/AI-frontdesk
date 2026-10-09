from uuid import uuid4

import pytest
from sqlalchemy import select

from app.agents.context import AgentContext
from app.agents.runtime import AgentRuntime
from app.agents.tools import directory_tools
from app.audit.models import AuditEvent
from app.calls.directory import CallDirectoryService, DirectoryQuery
from app.calls.models import PhoneNumber
from app.calls.service import CallContext, CallService
from app.conversation.manager import ConversationStateManager
from app.conversation.store import RedisConversationStateStore
from app.core.database.rls import set_tenant_scope
from app.core.errors import AuthorizationError
from app.core.redis import RedisClient
from app.locations.models import Location
from app.providers.models import Provider
from tests.fake_llm import MockLLMProvider
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_agent_tools_use_active_call_scope_and_audit(tenancy: TenancyFixture) -> None:
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
        a = Location(tenant_id=tenancy.a.tenant_id, name="Clinic A", timezone="Asia/Kolkata")
        b = Location(tenant_id=tenancy.b.tenant_id, name="Private tenant B", timezone="UTC")
        session.add_all([a, b])
        await session.flush()
        session.add(
            Provider(
                tenant_id=tenancy.a.tenant_id,
                location_id=a.id,
                display_name="Dr Shah",
                specialty="General",
            )
        )
    calls = CallService(tenancy.database.session_factory)
    call = await calls.receive(number, "CA" + uuid4().hex, uuid4())
    service = CallDirectoryService(tenancy.database.session_factory)
    with pytest.raises(AuthorizationError):
        await service.read(call, "locations", DirectoryQuery())
    await calls.start(call, "MZ" + uuid4().hex)
    state = ConversationStateManager.create_initial_state(
        call.call_id, call.tenant_id, call.provider_call_id
    )
    runtime = AgentRuntime(MockLLMProvider(), directory_tools(service))
    result = await runtime.respond(AgentContext(call), state, "Which clinic locations?")
    assert result.status == "completed" and "Clinic A" in result.response
    assert "Private tenant B" not in result.response
    doctors = await runtime.respond(AgentContext(call), state, "Which doctors?")
    assert "Dr Shah" in doctors.response and doctors.tools_used == ["list_providers"]
    forged = CallContext(tenancy.b.tenant_id, call.call_id, call.provider_call_id, uuid4())
    with pytest.raises(AuthorizationError):
        await service.read(forged, "providers", DirectoryQuery())
    async with tenancy.database.session_factory.begin() as session:
        await set_tenant_scope(session, call.tenant_id)
        events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.call_id == call.call_id, AuditEvent.action.like("agent_directory_%")
                )
            )
        ).all()
        assert len(events) == 2
    await calls.finish(call, "completed", "test_complete")
    with pytest.raises(AuthorizationError):
        await service.read(call, "locations", DirectoryQuery())


async def test_real_redis_conversation_roundtrip_ttl_and_scope(tenancy: TenancyFixture) -> None:
    redis = RedisClient(tenancy.settings)
    store = RedisConversationStateStore(redis.client)
    state = ConversationStateManager.create_initial_state(uuid4(), tenancy.a.tenant_id, "CAtest")
    try:
        await store.save(state, 65)
        assert (
            0
            < await redis.client.ttl(f"tenant:{state.tenant_id}:call:{state.call_id}:conv_state")
            <= 65
        )
        assert await store.get(tenancy.b.tenant_id, state.call_id) is None
        loaded = await store.get(state.tenant_id, state.call_id)
        assert loaded is not None and loaded.call_id == state.call_id
    finally:
        await store.delete(state.tenant_id, state.call_id)
        await redis.close()
