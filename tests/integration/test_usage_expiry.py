"""Real role boundaries, transaction rollback and competing expiry workers."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.billing.database import FinancialDatabase, financial_transaction, lock_financial_tenant
from app.billing.expiry import UsageExpiryBatchError, UsageExpirySweeper, verify_discovery_role
from app.billing.models import BillingLedger
from app.billing.service import BillingService
from app.billing.usage import expire_reservations
from app.billing.usage_models import UsageReservation
from app.billing.usage_schemas import ChangeUsage, SettleUsage
from app.database.rls import UnsafeDatabaseRoleError, set_tenant_scope
from app.database.session import Database
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.integration.test_usage_reservations import funded, reserve_command


@pytest.fixture
async def financial(tenancy: TenancyFixture) -> AsyncGenerator[FinancialDatabase]:
    database = FinancialDatabase(tenancy.settings.model_copy(update={"db_pool_size": 4}))
    await database.ping()
    try:
        yield database
    finally:
        await database.close()


def discovery(tenancy: TenancyFixture) -> Database:
    assert tenancy.settings.worker_database_url is not None
    return Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )


async def test_expiry_discovery_cannot_read_or_change_finances(tenancy: TenancyFixture) -> None:
    worker = discovery(tenancy)
    try:
        async with worker.session_factory.begin() as session:
            await verify_discovery_role(session)
            await session.execute(select(UsageReservation.tenant_id).limit(1))
        for statement in (
            "SELECT quoted_minor FROM usage_reservations",
            "SELECT fence FROM usage_reservations",
            "SELECT balance_minor FROM billing_accounts",
            "SELECT * FROM usage_commands",
            "UPDATE usage_reservations SET state = 'released'",
        ):
            with pytest.raises(DBAPIError):
                async with worker.session_factory.begin() as session:
                    await session.execute(text(statement))
        with pytest.raises(UnsafeDatabaseRoleError):
            async with tenancy.database.session_factory.begin() as session:
                await verify_discovery_role(session)
    finally:
        await worker.close()


async def test_competing_sweepers_release_once_and_keep_unknown_dispatch_funds(
    tenancy: TenancyFixture, financial: FinancialDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, system, usage, _ = await funded(tenancy, financial)
    running = await usage.reserve(system, reserve_command())
    # Exercise actual database time without sleeping for production lease durations.
    monkeypatch.setattr("app.billing.usage.WORKER_LEASE", timedelta(seconds=-1))
    started = await usage.start(
        system, ChangeUsage(mutation_id=uuid4(), reservation_id=running.id, revision=1)
    )
    monkeypatch.setattr("app.billing.usage.HOLD_LEASE", timedelta(seconds=-1))
    unused = await usage.reserve(system, reserve_command())
    worker = discovery(tenancy)
    try:
        sweeper = UsageExpirySweeper(worker.session_factory, financial.session_factory)
        await sweeper.check()
        counts = await asyncio.gather(
            *(sweeper.process_batch(tenant_id=owner.tenant_id) for _ in range(3))
        )
        assert sum(counts) == 2
        assert await sweeper.process_batch(tenant_id=owner.tenant_id) == 0
        assert await sweeper.process_batch(tenant_id=tenancy.b.tenant_id) == 0
        totals = await usage.get_summary(owner)
        assert totals.reserved_minor == 200 and totals.reconciliation_count == 1
        assert totals.voice_slots_reserved == 1
        async with financial.session_factory.begin() as session:
            await set_tenant_scope(session, owner.tenant_id)
            released = await session.get(UsageReservation, unused.id)
            assert released is not None and released.state == "released"
            assert released.outcome_reference == "unused-lease-expired"
            count = await session.scalar(
                select(func.count())
                .select_from(BillingLedger)
                .where(BillingLedger.kind == "release")
            )
            assert count == 1
        assert started.fence is not None
        await usage.settle(
            system,
            SettleUsage(
                mutation_id=uuid4(),
                reservation_id=running.id,
                revision=3,
                fence=started.fence,
                actual_units=25,
                outcome_reference="verified-fixture-end",
            ),
        )
        assert (
            await BillingService(tenancy.database.session_factory).get(owner)
        ).balance_minor == 9950
    finally:
        await worker.close()


async def test_expiry_skips_busy_tenant_and_recovers_after_transaction_rollback(
    tenancy: TenancyFixture, financial: FinancialDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, system, usage, clock = await funded(tenancy, financial)
    monkeypatch.setattr("app.billing.usage.HOLD_LEASE", timedelta(seconds=-1))
    held = await usage.reserve(system, reserve_command())
    worker = discovery(tenancy)
    try:
        sweeper = UsageExpirySweeper(worker.session_factory, financial.session_factory)
        with pytest.raises(RuntimeError, match="simulated crash"):
            async with financial_transaction(financial.session_factory) as writer:
                await set_tenant_scope(writer, owner.tenant_id)
                await lock_financial_tenant(writer, owner.tenant_id)
                assert await sweeper.process_batch(tenant_id=owner.tenant_id) == 0
                assert await expire_reservations(writer, system, clock.now) == 1
                raise RuntimeError("simulated crash before commit")
        assert (await usage.get_summary(owner)).reserved_minor == 200
        assert await sweeper.process_batch(tenant_id=owner.tenant_id) == 1
        assert (await usage.get_summary(owner)).reserved_minor == 0
        async with financial.session_factory.begin() as session:
            await set_tenant_scope(session, owner.tenant_id)
            row = await session.get(UsageReservation, held.id)
            assert row is not None and row.revision == 2
    finally:
        await worker.close()


async def test_failed_expiry_reports_failure_and_retries_durable_work(
    tenancy: TenancyFixture, financial: FinancialDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, system, usage, _ = await funded(tenancy, financial)
    monkeypatch.setattr("app.billing.usage.HOLD_LEASE", timedelta(seconds=-1))
    await usage.reserve(system, reserve_command())

    async def interrupted(writer: AsyncSession, context: RequestContext, now: datetime) -> int:
        await expire_reservations(writer, context, now)
        raise SQLAlchemyError("simulated connection failure after flush")

    worker = discovery(tenancy)
    try:
        sweeper = UsageExpirySweeper(worker.session_factory, financial.session_factory)
        with monkeypatch.context() as patch:
            patch.setattr("app.billing.expiry.expire_reservations", interrupted)
            with pytest.raises(UsageExpiryBatchError) as failure:
                await sweeper.process_batch(tenant_id=system.tenant_id)
            assert failure.value.failed == 1
        assert (await usage.get_summary(owner)).reserved_minor == 200
        assert await sweeper.process_batch(tenant_id=system.tenant_id) == 1
        assert (await usage.get_summary(owner)).reserved_minor == 0
    finally:
        await worker.close()
