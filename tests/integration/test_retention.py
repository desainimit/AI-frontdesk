"""Old call transcripts are deleted by each clinic's own retention setting."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select, update

from app.calls.models import Call, PhoneNumber
from app.core.database.rls import set_tenant_scope
from app.core.database.session import Database
from app.maintenance.retention import sweep_transcripts
from app.postcall.models import CallTranscript
from app.settings.schemas import PrivacySettings, SettingsSection
from app.settings.service import SettingsService
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def transcript(tenancy: TenancyFixture, tenant_id: UUID, age_days: int) -> UUID:
    """A finished call with a transcript saved this many days ago."""
    identity = uuid4()
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenant_id)
        phone = PhoneNumber(tenant_id=tenant_id, number="+1" + str(uuid4().int)[:10])
        session.add(phone)
        await session.flush()
        call = Call(
            tenant_id=tenant_id,
            phone_number_id=phone.id,
            provider_call_id="CA" + uuid4().hex,
            status="completed",
        )
        session.add(call)
        await session.flush()
        session.add(
            CallTranscript(
                id=identity, tenant_id=tenant_id, call_id=call.id, snapshot={"turns": []}
            )
        )
        await session.flush()
        await session.execute(
            update(CallTranscript)
            .where(CallTranscript.id == identity)
            .values(created_at=datetime.now(UTC) - timedelta(days=age_days))
        )
    return identity


async def test_worker_removes_transcripts_past_each_clinics_retention(
    tenancy: TenancyFixture,
) -> None:
    assert isinstance(tenancy.settings.worker_database_url, SecretStr)
    # Clinic A keeps transcripts 30 days; clinic B keeps the default 90.
    await SettingsService(tenancy.database.session_factory).save(
        tenancy.context(tenancy.a),
        SettingsSection.PRIVACY,
        PrivacySettings(transcript_retention_days=30).model_dump(mode="json"),
    )
    old_a = await transcript(tenancy, tenancy.a.tenant_id, 45)
    recent_a = await transcript(tenancy, tenancy.a.tenant_id, 5)
    old_b = await transcript(tenancy, tenancy.b.tenant_id, 45)
    worker = Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    try:
        assert await sweep_transcripts(worker.session_factory) is False
    finally:
        await worker.close()
    async with tenancy.admin.begin() as session:
        kept = set(
            await session.scalars(
                select(CallTranscript.id).where(CallTranscript.id.in_([old_a, recent_a, old_b]))
            )
        )
    assert kept == {recent_a, old_b}
