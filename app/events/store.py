from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.core.database.rls import UnsafeDatabaseRoleError
from app.events.models import OutboxEvent


async def verify_worker_role(session: AsyncSession) -> None:
    safe = await session.scalar(
        text("""
        SELECT NOT (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)
          AND NOT pg_has_role(current_user, c.relowner, 'MEMBER')
          AND c.relrowsecurity AND c.relforcerowsecurity
          AND has_table_privilege(current_user, 'public.outbox_events', 'SELECT')
          AND has_column_privilege(current_user, 'public.outbox_events', 'status', 'UPDATE')
          AND NOT has_table_privilege(current_user, 'public.patients', 'SELECT')
        FROM pg_roles, pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE rolname = current_user AND n.nspname = 'public' AND c.relname = 'outbox_events'
    """)
    )
    if safe is not True:
        raise UnsafeDatabaseRoleError("Use the dedicated, restricted outbox worker role")


@dataclass(frozen=True)
class ClaimedEvent:
    id: UUID
    tenant_id: UUID
    event_type: str
    schema_version: int
    payload: dict[str, object]
    attempts: int
    lease_token: UUID


class OutboxStore:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], lease_seconds: float = 30
    ) -> None:
        self._sessions, self._lease_seconds = sessions, lease_seconds

    async def check(self) -> None:
        async with self._sessions.begin() as session:
            await verify_worker_role(session)

    async def claim(self) -> ClaimedEvent | None:
        async with self._sessions.begin() as session:
            await verify_worker_role(session)
            older = aliased(OutboxEvent)
            blocked = (
                select(older.id)
                .where(
                    older.tenant_id == OutboxEvent.tenant_id,
                    older.aggregate_id == OutboxEvent.aggregate_id,
                    older.aggregate_version < OutboxEvent.aggregate_version,
                    older.status.in_(("pending", "processing")),
                )
                .exists()
            )
            row = await session.scalar(
                select(OutboxEvent)
                .where(
                    ~blocked,
                    or_(
                        and_(
                            OutboxEvent.status == "pending", OutboxEvent.available_at <= func.now()
                        ),
                        and_(
                            OutboxEvent.status == "processing",
                            OutboxEvent.lease_until <= func.now(),
                        ),
                    ),
                )
                .order_by(OutboxEvent.available_at, OutboxEvent.created_at, OutboxEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            token = uuid4()
            # Database time avoids clock skew between workers.
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == row.id)
                .values(
                    status="processing",
                    attempts=OutboxEvent.attempts + 1,
                    lease_token=token,
                    lease_until=func.now() + timedelta(seconds=self._lease_seconds),
                )
                .execution_options(synchronize_session=False)
            )
            return ClaimedEvent(
                row.id,
                row.tenant_id,
                row.event_type,
                row.schema_version,
                row.payload,
                row.attempts + 1,
                token,
            )

    async def finish(
        self, event: ClaimedEvent, *, error: str | None = None, retry_in: float | None = None
    ) -> bool:
        async with self._sessions.begin() as session:
            values: dict[str, Any] = {
                "lease_token": None,
                "lease_until": None,
                "last_error": error,
            }
            if error is None:
                values.update(status="delivered", delivered_at=func.now())
            elif retry_in is None:
                values.update(status="dead")
            else:
                values.update(
                    status="pending", available_at=func.now() + timedelta(seconds=retry_in)
                )
            updated = await session.scalar(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event.id,
                    OutboxEvent.lease_token == event.lease_token,
                    OutboxEvent.status == "processing",
                    OutboxEvent.lease_until > func.now(),
                )
                .values(**values)
                .returning(OutboxEvent.id)
            )
            return updated is not None
