"""Disposable-PostgreSQL checks for tenant-scoped transfer configuration."""

from dataclasses import replace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.audit.models import AuditEvent
from app.calls.service import CallService
from app.errors import NotFoundError
from app.main import create_app
from tests.fakes import FakeDependency
from tests.integration.test_appointment_agent import setup_call
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_transfer_destination_admin_api_and_call_scope(tenancy: TenancyFixture) -> None:
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    endpoint = "/api/v1/tenants/current/transfer"
    headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        saved = await client.put(endpoint, json={"number": "+15551234567"})
        assert saved.status_code == 200
        assert saved.json()["human_transfer_number"] == "+15551234567"
        assert (await client.put(endpoint, json={"number": "12345"})).status_code == 422
        assert (
            await client.put(
                endpoint,
                json={"number": "+15551234568"},
                headers={"X-Tenant-ID": str(tenancy.b.tenant_id)},
            )
        ).status_code == 403
        other = await client.get(
            "/api/v1/tenants/current",
            headers={"Authorization": f"Bearer {tenancy.b.token.get_secret_value()}"},
        )
        assert other.json()["human_transfer_number"] is None
        _, context, _, _, _ = await setup_call(tenancy)
        calls = CallService(tenancy.database.session_factory)
        assert await calls.transfer_number(context.call) == "+15551234567"
        with pytest.raises(NotFoundError):
            await calls.transfer_number(replace(context.call, tenant_id=tenancy.b.tenant_id))
        async with tenancy.admin.begin() as session:
            events = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenancy.a.tenant_id,
                        AuditEvent.action == "tenant_transfer_updated",
                    )
                )
            ).all()
            assert len(events) == 1 and events[0].actor_id == tenancy.a.user_id
        cleared = await client.put(endpoint, json={"number": None})
        assert cleared.status_code == 200
        assert await calls.transfer_number(context.call) is None
