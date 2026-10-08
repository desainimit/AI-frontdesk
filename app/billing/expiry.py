"""Discover overdue leases without exposing financial contents to the job login."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActorType, RequestContext
from app.billing.database import (
    FINANCIAL_TABLES,
    financial_transaction,
    lock_financial_tenant,
)
from app.billing.usage import expire_reservations
from app.billing.usage_models import UsageReservation
from app.database.rls import UnsafeDatabaseRoleError, set_tenant_scope
from app.events.store import verify_worker_role


class UsageExpiryBatchError(RuntimeError):
    """Other tenant transactions can commit even when one tenant cannot be processed."""

    def __init__(self, failed: int) -> None:
        self.failed = failed
        super().__init__("Some usage-expiry transactions failed; retry from durable state")


async def verify_discovery_role(session: AsyncSession) -> None:
    await verify_worker_role(session)
    unsafe = await session.scalar(
        text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            WHERE n.nspname = 'public' AND c.relname = ANY(CAST(:tables AS text[]))
            AND (has_table_privilege(current_user, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE')
                OR has_column_privilege(current_user, c.oid, a.attnum, 'INSERT,UPDATE')
                OR (has_column_privilege(current_user, c.oid, a.attnum, 'SELECT') AND NOT (
                    c.relname = 'usage_reservations'
                    AND a.attname IN ('tenant_id', 'state', 'lease_expires_at'))))
        ) OR NOT (
            has_column_privilege(current_user, 'usage_reservations', 'tenant_id', 'SELECT')
            AND has_column_privilege(current_user, 'usage_reservations', 'state', 'SELECT')
            AND has_column_privilege(current_user, 'usage_reservations',
                'lease_expires_at', 'SELECT')
        ) OR NOT EXISTS (
            SELECT 1 FROM pg_class WHERE oid = 'public.usage_reservations'::regclass
            AND relrowsecurity AND relforcerowsecurity
            AND NOT pg_has_role(current_user, relowner, 'MEMBER')
        )
        """),
        {"tables": list(FINANCIAL_TABLES)},
    )
    if unsafe is not False:
        raise UnsafeDatabaseRoleError("Usage discovery requires metadata-only financial access")


class UsageExpirySweeper:
    def __init__(
        self,
        discovery_sessions: async_sessionmaker[AsyncSession],
        financial_sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self._discovery, self._financial = discovery_sessions, financial_sessions

    async def check(self) -> None:
        async with self._discovery.begin() as session:
            await verify_discovery_role(session)
        async with financial_transaction(self._financial):
            pass

    async def process_batch(self, *, tenant_id: UUID | None = None) -> int:
        # Discovery is advisory, never a dispatch or financial authorization.
        # Release this connection before acquiring any financial mutex.
        async with self._discovery.begin() as session:
            await verify_discovery_role(session)
            query = (
                select(UsageReservation.tenant_id)
                .where(
                    UsageReservation.state.in_(("held", "running")),
                    UsageReservation.lease_expires_at <= func.now(),
                )
                .group_by(UsageReservation.tenant_id)
                .order_by(func.min(UsageReservation.lease_expires_at), UsageReservation.tenant_id)
                .limit(20)
            )
            if tenant_id is not None:
                query = query.where(UsageReservation.tenant_id == tenant_id)
            tenants = list(await session.scalars(query))
        processed, failed = 0, 0
        for identity in tenants:
            try:
                async with financial_transaction(self._financial) as writer:
                    await set_tenant_scope(writer, identity)
                    if not await lock_financial_tenant(writer, identity, wait=False):
                        continue
                    now = await writer.scalar(select(func.clock_timestamp()))
                    if not isinstance(now, datetime):
                        raise RuntimeError("Database time unavailable")
                    context = RequestContext(uuid4(), identity, None, ActorType.SYSTEM)
                    # Re-read under the mutex; discovery rows cannot authorize expiry.
                    count = await expire_reservations(writer, context, now)
                processed += count
            except (SQLAlchemyError, OSError, TimeoutError):
                # A tenant with an invariant failure must not prevent the rest of
                # the discovered tenants from progressing. Never log SQL or payloads.
                failed += 1
        if failed:
            raise UsageExpiryBatchError(failed)
        return processed
