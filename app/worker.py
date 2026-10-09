"""The one background process: every queue is drained here under the worker login.

Run: python -m app.worker. WORKER_DATABASE_URL must be the restricted worker login,
never the API login or the schema owner.
"""

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from types import FrameType

from redis.exceptions import RedisError
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

# Every table must be registered in this process: queue rows reference tables whose
# models no queue imports by itself.
import app.core.database.models  # noqa: F401  # pyright: ignore[reportUnusedImport]
from app.auth.mail_worker import deliver_one
from app.configuration.dispatch import ConfigurationDispatcher, ValkeyRevisionPublisher
from app.core.database.session import Database
from app.core.logging import configure_logging
from app.core.redis import RedisClient
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.core.telemetry import Telemetry
from app.events.processor import EventProcessor
from app.events.providers import ConsoleNotificationProvider, RetryPolicy
from app.events.store import OutboxStore, verify_worker_role
from app.integrations.resolver import TenantIntegrationResolver
from app.knowledge.embeddings import open_embedder
from app.knowledge.storage import build_storage
from app.knowledge.worker import KnowledgeWorker
from app.workflows.activities import WorkflowActivities
from app.workflows.runner import JobRunner
from app.workflows.store import WorkflowIntentStore

logger = logging.getLogger("app.worker")

Step = Callable[[], Awaitable[bool]]


@dataclass(frozen=True)
class Queue:
    """One source of work. ``step`` returns False when there is nothing to do now."""

    name: str
    step: Step
    idle_seconds: float


async def drain(queue: Queue, stop: asyncio.Event, *, once: bool = False) -> None:
    while not stop.is_set():
        try:
            worked = await queue.step()
        except (DBAPIError, PoolTimeoutError, RedisError, OSError, TimeoutError) as failure:
            # An unreachable dependency leaves leases recoverable; the queue retries.
            # Anything else, including a mapping error, is a defect and must surface.
            logger.error(
                "worker_dependency_unavailable",
                extra={"dependency": queue.name, "error_type": type(failure).__name__},
            )
            worked = False
        if once:
            return
        if not worked:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=queue.idle_seconds)


async def build_queues(settings: Settings, stack: AsyncExitStack) -> list[Queue]:
    """Open shared resources and verify the login before any queue is touched."""
    if settings.worker_database_url is None:
        raise ValueError("Configure a separate WORKER_DATABASE_URL")
    database = Database(settings.model_copy(update={"database_url": settings.worker_database_url}))
    stack.push_async_callback(database.close)
    redis = RedisClient(settings)
    stack.push_async_callback(redis.close)
    telemetry = Telemetry(settings, "ai-frontdesk-worker")
    stack.push_async_callback(telemetry.close)
    sessions = database.session_factory

    outbox = OutboxStore(sessions, 90)
    await outbox.check()
    events = EventProcessor(
        outbox,
        ConsoleNotificationProvider(),
        retry=RetryPolicy(max_attempts=5),
        timeout_seconds=10,
        telemetry=telemetry,
    )
    jobs = JobRunner(WorkflowIntentStore(sessions), WorkflowActivities(sessions))
    configuration = ConfigurationDispatcher(
        sessions,
        ValkeyRevisionPublisher(redis),
        timeout_seconds=settings.dependency_timeout_seconds,
    )
    await configuration.check()
    await redis.ping()
    queues = [
        Queue("outbox", events.process_one, 1),
        Queue("jobs", jobs.process_one, 1),
        Queue("configuration", configuration.process_one, 1),
    ]
    if settings.mail_enabled:
        # Fail at start, not on the first message, when the keyring is unusable.
        SecretCipher(settings.secret_encryption_keys, settings.secret_encryption_active_key)
        queues.append(Queue("mail", lambda: deliver_one(sessions, settings), 2))
    # Each job opens the embedder for its own tenant, so the worker starts even when no
    # model key is configured yet; such a job fails visibly instead.
    resolver = TenantIntegrationResolver(sessions, settings, role_check=verify_worker_role)
    knowledge = KnowledgeWorker(
        sessions,
        build_storage(settings),
        None,
        tenant_embedder=lambda tenant_id: open_embedder(settings, resolver, tenant_id),
    )
    queues.append(Queue("knowledge", knowledge.process_one, 2))
    return queues


async def run(settings: Settings, *, once: bool = False, check: bool = False) -> None:
    stop = asyncio.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)
    async with AsyncExitStack() as stack:
        queues = await build_queues(settings, stack)
        if check:
            return
        logger.info("worker_started")
        try:
            # A queue that fails outside the handled dependency errors stops the
            # process, so the orchestrator restarts it instead of running half a worker.
            async with asyncio.TaskGroup() as group:
                for queue in queues:
                    group.create_task(drain(queue, stop, once=once), name=queue.name)
        finally:
            logger.info("worker_stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run each queue once and exit")
    parser.add_argument("--check", action="store_true", help="verify access and exit")
    args = parser.parse_args()
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(settings, once=args.once, check=args.check))
    except BaseException as failure:
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise
        # Exception text can carry connection strings; only the type is logged.
        logger.error("worker_failed", extra={"error_type": type(failure).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
