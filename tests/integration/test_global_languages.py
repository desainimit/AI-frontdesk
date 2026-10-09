from dataclasses import replace
from uuid import uuid4, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.agents.appointment_flow import BookInput
from app.agents.language import spoken
from app.audit.models import AuditEvent
from app.calls.service import CallService
from app.conversation.locales import LanguagePolicy
from app.conversation.manager import ConversationStateManager
from app.database.unit_of_work import UnitOfWork
from app.errors import AuthorizationError, NotFoundError
from app.main import create_app
from app.tenants.models import Tenant
from app.tenants.service import TenantService
from app.users.models import User
from tests.fakes import FakeDependency
from tests.integration.test_appointment_agent import count_appointments, identify, setup_call
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.test_global_languages import CONSENT


async def test_language_policy_api_rbac_audit_and_isolation(tenancy: TenancyFixture) -> None:
    app = create_app(
        tenancy.settings,
        redis_factory=lambda _: FakeDependency(),
        rate_limits=False,
    )
    headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
    policy = {"default_language": "es", "enabled_languages": ["es", "en"], "auto_detect": True}
    endpoint = "/api/v1/tenants/current/languages"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        saved = await client.put(endpoint, json=policy)
        assert saved.status_code == 200 and saved.json()["language_policy"] == policy
        assert (await client.get("/api/v1/tenants/current")).json()["language_policy"] == policy
        assert (
            await client.put(endpoint, json={**policy, "default_language": "ja"})
        ).status_code == 422
        assert (
            await client.put(
                endpoint, json=policy, headers={"X-Tenant-ID": str(tenancy.b.tenant_id)}
            )
        ).status_code == 403
        other = await client.get(
            "/api/v1/tenants/current",
            headers={"Authorization": f"Bearer {tenancy.b.token.get_secret_value()}"},
        )
        assert other.json()["language_policy"]["default_language"] == "en"
        async with tenancy.admin.begin() as session:
            events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenancy.a.tenant_id,
                        AuditEvent.action == "tenant_languages_updated",
                    )
                )
            ).all()
            assert len(events) == 1 and events[0].actor_id == tenancy.a.user_id
            await session.execute(
                update(User)
                .where(User.id == tenancy.a.user_id)
                .values(role="member", role_id=uuid5(tenancy.a.tenant_id, "role:member"))
            )
        assert (await client.put(endpoint, json=policy)).status_code == 403
    # A service call with a stale admin context is rejected as well.
    service = TenantService(UnitOfWork(tenancy.database.session_factory))
    with pytest.raises(AuthorizationError):
        await service.set_language_policy(tenancy.context(tenancy.a), LanguagePolicy())
    async with tenancy.admin.begin() as session:
        tenant = await session.get(Tenant, tenancy.a.tenant_id)
        assert tenant is not None and tenant.language_policy == policy


async def test_call_loads_only_its_tenant_policy(tenancy: TenancyFixture) -> None:
    service = TenantService(UnitOfWork(tenancy.database.session_factory))
    policy = LanguagePolicy(
        default_language="fr", enabled_languages=["fr", "en"], auto_detect=False
    )
    await service.set_language_policy(tenancy.context(tenancy.a), policy)
    _, context, _, _, _ = await setup_call(tenancy)
    calls = CallService(tenancy.database.session_factory)
    assert await calls.language_policy(context.call) == policy
    with pytest.raises(NotFoundError):
        await calls.language_policy(replace(context.call, tenant_id=tenancy.b.tenant_id))
    with pytest.raises(NotFoundError):
        await calls.language_policy(replace(context.call, call_id=uuid4()))


@pytest.mark.parametrize("code,yes,no,revised", CONSENT)
async def test_localized_booking_requires_fresh_exact_consent(
    tenancy: TenancyFixture, code: str, yes: str, no: str, revised: str
) -> None:
    domain, context, state, registry, flow = await setup_call(tenancy)
    state.language_state.primary_language = code
    state.language_state.policy.auto_detect = False
    await identify(tenancy, domain.patient_id, context, state, registry)
    payload = BookInput(provider_id=domain.provider_id, starts_at=domain.start)
    for rejected in [no, revised]:
        await flow.propose(context, state, "book_appointment", payload)
        ConversationStateManager.process_caller_utterance(state, rejected)
        await flow.before_turn(context, state, rejected)
        assert state.appointment_operation is None and await count_appointments(tenancy) == 0
        assert await flow.before_turn(context, state, yes) is None
        assert await count_appointments(tenancy) == 0
    await flow.propose(context, state, "book_appointment", payload)
    ConversationStateManager.process_caller_utterance(state, yes)
    result = await flow.before_turn(context, state, yes)
    assert result is not None
    assert result.response == spoken(code, "booked", time=domain.start.isoformat())
    assert await count_appointments(tenancy) == 1
