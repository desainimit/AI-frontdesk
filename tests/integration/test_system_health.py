"""The health page: the worker reports, the administrator reads, problems are named."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import delete, update

from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.notifications.models import OutboundMessage
from app.platform.health import SystemHealthService, record_worker_status
from app.platform.models import PlatformStatus
from app.platform.policies import PlatformContext, PlatformRole
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.integration.workspaces import create_platform_admin


async def test_the_worker_reports_and_the_administrator_sees_what_is_wrong(
    tenancy: TenancyFixture,
) -> None:
    signed_in = await create_platform_admin(tenancy.admin)
    administrator = PlatformContext(
        signed_in.account_id, signed_in.session_id, uuid4(), PlatformRole.SUPER_ADMIN
    )
    health = SystemHealthService(tenancy.database.session_factory)
    assert isinstance(tenancy.settings.worker_database_url, SecretStr)
    worker = Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    try:
        # The status row is shared by every clinic, so start from none.
        async with tenancy.admin.begin() as session:
            await session.execute(delete(PlatformStatus))
        silent = await health.read(administrator)
        assert silent.status == "problem"
        assert (silent.items[0].key, silent.items[0].status) == ("worker", "problem")

        # A message that has waited too long is the worker's to notice.
        async with tenancy.admin.begin() as session:
            await set_tenant_scope(session, tenancy.a.tenant_id)
            session.add(
                OutboundMessage(
                    tenant_id=tenancy.a.tenant_id,
                    channel="sms",
                    kind="reply",
                    recipient="+919812345678",
                    sender="+14155550100",
                    body="Late",
                    dedupe_key=f"health-{uuid4().hex}",
                    available_at=datetime.now(UTC) - timedelta(minutes=30),
                )
            )
        await record_worker_status(worker.session_factory)
        seen = {item.key: item for item in (await health.read(administrator)).items}
        assert seen["worker"].status == "ok"
        assert seen["messages"].status == "problem" and "waiting" in seen["messages"].detail
        assert seen["account_email"].status == "ok" and seen["texts"].status == "ok"
        # Replies are counted across clinics, so other tests may have added some.
        assert "last hour" in seen["receptionist"].detail

        # A worker that stops writing is itself the problem.
        async with tenancy.admin.begin() as session:
            await session.execute(
                update(PlatformStatus).values(
                    worker_seen_at=datetime.now(UTC) - timedelta(minutes=12)
                )
            )
        stopped = await health.read(administrator)
        assert stopped.items[0].status == "problem" and "12 minutes ago" in stopped.items[0].detail
    finally:
        await worker.close()
        async with tenancy.admin.begin() as session:
            await set_tenant_scope(session, tenancy.a.tenant_id)
            await session.execute(
                delete(OutboundMessage).where(OutboundMessage.tenant_id == tenancy.a.tenant_id)
            )
            await session.execute(delete(PlatformStatus))
