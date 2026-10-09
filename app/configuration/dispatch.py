"""At-least-once invalidation from committed PostgreSQL publication events.

Valkey is a notification transport. Authoritative heads remain in PostgreSQL;
readers must reconcile on reconnect because the transport can lose all state.
"""

import asyncio
import json
from collections.abc import Awaitable
from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.configuration.models import ConfigurationEvent
from app.core.database.rls import UnsafeDatabaseRoleError
from app.core.redis import RedisClient
from app.events.store import verify_worker_role


class RevisionPublisher(Protocol):
    async def publish(self, tenant_id: UUID, version: int, event_id: UUID) -> None: ...


class ValkeyRevisionPublisher:
    def __init__(self, redis: RedisClient) -> None:
        self.redis = redis

    async def publish(self, tenant_id: UUID, version: int, event_id: UUID) -> None:
        payload = json.dumps(
            {"event_id": str(event_id), "tenant_id": str(tenant_id), "version": version}
        )
        # Duplicate/out-of-order delivery never regresses the ephemeral head.
        script = """
        local previous = tonumber(redis.call('GET', KEYS[1]) or '0')
        local version = tonumber(ARGV[1])
        if version > previous then
          redis.call('SET', KEYS[1], ARGV[1], 'EX', 86400)
          redis.call('PUBLISH', KEYS[2], ARGV[2])
        end
        return 1
        """
        result = cast(
            Awaitable[object],
            self.redis.client.eval(  # pyright: ignore[reportUnknownMemberType]
                script,
                2,
                f"configuration:head:{tenant_id}",
                f"configuration:published:{tenant_id}",
                str(version),
                payload,
            ),
        )
        if await result != 1:
            raise ConnectionError("Configuration invalidation transport unavailable")


async def verify_configuration_worker(session: AsyncSession) -> None:
    await verify_worker_role(session)
    safe = await session.scalar(
        text("""
        SELECT c.relrowsecurity AND c.relforcerowsecurity
          AND NOT pg_has_role(current_user, c.relowner, 'MEMBER')
          AND has_table_privilege(current_user, 'public.configuration_events', 'SELECT')
          AND has_column_privilege(current_user,
                'public.configuration_events', 'delivered_at', 'UPDATE')
          AND NOT has_column_privilege(current_user,
                'public.configuration_events', 'version', 'UPDATE')
          AND NOT has_table_privilege(current_user, 'public.published_configurations', 'SELECT')
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relname='configuration_events'
    """)
    )
    if safe is not True:
        raise UnsafeDatabaseRoleError("Provision the restricted configuration event worker")


class ConfigurationDispatcher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        publisher: RevisionPublisher,
        *,
        timeout_seconds: float = 2,
    ) -> None:
        self.sessions, self.publisher, self.timeout_seconds = sessions, publisher, timeout_seconds

    async def check(self) -> None:
        async with self.sessions.begin() as session:
            await verify_configuration_worker(session)

    async def process_one(self, *, tenant_id: UUID | None = None) -> bool:
        # A bounded transport call holds one event lock. Crashes and failed commits
        # leave the durable event pending; SKIP LOCKED allows multiple workers.
        async with self.sessions.begin() as session:
            await verify_configuration_worker(session)
            query = select(ConfigurationEvent).where(
                ConfigurationEvent.delivered_at.is_(None),
                ConfigurationEvent.available_at <= func.now(),
            )
            if tenant_id is not None:
                query = query.where(ConfigurationEvent.tenant_id == tenant_id)
            event = await session.scalar(
                query.order_by(
                    ConfigurationEvent.available_at,
                    ConfigurationEvent.created_at,
                    ConfigurationEvent.version,
                    ConfigurationEvent.id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                return False
            await asyncio.wait_for(
                self.publisher.publish(event.tenant_id, event.version, event.id),
                timeout=self.timeout_seconds,
            )
            # Raw statement updates only the acknowledgement column. The event
            # identity, tenant, version and publication binding remain immutable.
            await session.execute(
                text("UPDATE configuration_events SET delivered_at=now() WHERE id=:id"),
                {"id": event.id},
            )
            return True
