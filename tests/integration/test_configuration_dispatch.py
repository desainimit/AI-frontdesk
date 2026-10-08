"""Committed publication metadata survives dispatcher failure and transport loss."""

import asyncio
import json
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.cache.redis import RedisClient
from app.configuration.dispatch import ConfigurationDispatcher, ValkeyRevisionPublisher
from app.configuration.models import ConfigurationEvent, PublishedConfiguration
from app.configuration.schemas import SaveConfiguration
from app.configuration.service import ConfigurationService
from app.database.rls import set_tenant_scope
from app.database.session import Database
from tests.integration.test_saas import authenticate
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


class UnavailablePublisher:
    async def publish(self, tenant_id: UUID, version: int, event_id: UUID) -> None:
        raise ConnectionError("transport unavailable")


class BlockingPublisher:
    def __init__(self) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.count = 0

    async def publish(self, tenant_id: UUID, version: int, event_id: UUID) -> None:
        self.count += 1
        self.entered.set()
        await self.release.wait()


async def test_failed_dispatch_remains_pending_and_concurrent_workers_do_not_duplicate_claims(
    tenancy: TenancyFixture,
) -> None:
    assert tenancy.settings.worker_database_url is not None
    worker = Database(
        tenancy.settings.model_copy(
            update={
                "database_url": tenancy.settings.worker_database_url,
                "db_pool_size": 3,
            }
        )
    )
    try:
        context = await authenticate(tenancy)
        configuration = ConfigurationService(tenancy.database.session_factory)
        draft = await configuration.get(context)
        await configuration.publish(context, draft.revision)
        with pytest.raises(ConnectionError):
            await ConfigurationDispatcher(
                worker.session_factory, UnavailablePublisher()
            ).process_one(tenant_id=context.tenant_id)
        async with tenancy.admin.begin() as session:
            event = await session.scalar(
                select(ConfigurationEvent).where(ConfigurationEvent.tenant_id == context.tenant_id)
            )
            assert event is not None and event.delivered_at is None
        publisher = BlockingPublisher()
        dispatcher = ConfigurationDispatcher(worker.session_factory, publisher)
        first = asyncio.create_task(dispatcher.process_one(tenant_id=context.tenant_id))
        await asyncio.wait_for(publisher.entered.wait(), 2)
        try:
            assert not await dispatcher.process_one(tenant_id=context.tenant_id)
        finally:
            publisher.release.set()
        assert await first and publisher.count == 1
        assert not await dispatcher.process_one(tenant_id=context.tenant_id)
        async with tenancy.admin.begin() as session:
            event = await session.scalar(
                select(ConfigurationEvent).where(ConfigurationEvent.tenant_id == context.tenant_id)
            )
            assert event is not None and event.delivered_at is not None
    finally:
        await worker.close()


async def test_real_valkey_delivery_is_monotonic_and_database_head_survives_transport_loss(
    tenancy: TenancyFixture,
) -> None:
    assert tenancy.settings.worker_database_url is not None
    worker = Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    redis = RedisClient(tenancy.settings)
    subscription = redis.client.pubsub()  # pyright: ignore[reportUnknownMemberType]
    try:
        a, b = await authenticate(tenancy), await authenticate(tenancy, second=True)
        service = ConfigurationService(tenancy.database.session_factory)
        draft = await service.get(a)
        first = await service.publish(a, draft.revision)
        document = draft.document.model_copy(deep=True)
        document.agent.greeting = "Published transport recovery proof"
        saved = await service.save(a, SaveConfiguration(revision=draft.revision, document=document))
        second = await service.publish(a, saved.revision)
        channel = f"configuration:published:{a.tenant_id}"
        await subscription.subscribe(channel, f"configuration:published:{b.tenant_id}")  # pyright: ignore[reportUnknownMemberType]
        publisher = ValkeyRevisionPublisher(redis)
        dispatcher = ConfigurationDispatcher(worker.session_factory, publisher)
        assert await dispatcher.process_one(tenant_id=a.tenant_id)
        assert await dispatcher.process_one(tenant_id=a.tenant_id)
        messages: list[dict[str, object]] = []
        async with asyncio.timeout(4):
            while len(messages) < 2:
                message = cast(
                    dict[str, object] | None,
                    await subscription.get_message(ignore_subscribe_messages=True, timeout=1),
                )
                if message is not None:
                    assert message["channel"] == channel
                    data = message["data"]
                    assert isinstance(data, str | bytes)
                    messages.append(json.loads(data))
        assert [message["version"] for message in messages] == [first.version, second.version]
        assert all(set(message) == {"tenant_id", "version", "event_id"} for message in messages)
        # Replay older transport messages and then lose the ephemeral transport head.
        from uuid import uuid4

        await publisher.publish(a.tenant_id, first.version, uuid4())
        assert await redis.client.get(f"configuration:head:{a.tenant_id}") == str(second.version)
        await redis.client.delete(f"configuration:head:{a.tenant_id}")
        # Fresh API service objects read durable state even without dispatcher/caches.
        recovered = await ConfigurationService(tenancy.database.session_factory).head(a)
        assert (
            recovered.published_version == second.version
            and recovered.draft_revision == saved.revision
        )
        assert "document" not in recovered.model_dump()
        assert (await service.head(b)).published_version is None
    finally:
        await subscription.aclose()
        await redis.close()
        await worker.close()


async def test_dispatch_role_cannot_modify_event_identity_or_read_business_configuration(
    tenancy: TenancyFixture,
) -> None:
    assert tenancy.settings.worker_database_url is not None
    worker = Database(
        tenancy.settings.model_copy(update={"database_url": tenancy.settings.worker_database_url})
    )
    try:
        context = await authenticate(tenancy)
        configuration = ConfigurationService(tenancy.database.session_factory)
        draft = await configuration.get(context)
        await configuration.publish(context, draft.revision)
        for statement in (
            "UPDATE configuration_events SET version=version+1 WHERE tenant_id=:tenant",
            "DELETE FROM configuration_events WHERE tenant_id=:tenant",
            "SELECT document FROM published_configurations WHERE tenant_id=:tenant",
        ):
            async with worker.session_factory.begin() as session:
                await set_tenant_scope(session, context.tenant_id)
                with pytest.raises(DBAPIError):
                    await session.execute(text(statement), {"tenant": context.tenant_id})
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, context.tenant_id)
            with pytest.raises(DBAPIError):
                await session.execute(text("UPDATE configuration_events SET delivered_at=now()"))
        async with tenancy.admin.begin() as session:
            assert (
                await session.scalar(
                    select(PublishedConfiguration.version).where(
                        PublishedConfiguration.tenant_id == context.tenant_id
                    )
                )
                == 1
            )
    finally:
        await worker.close()
