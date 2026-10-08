"""Temporal worker and transactional intent dispatcher; never used in voice turns."""

import argparse
import asyncio
import logging
import signal
from types import FrameType

from temporalio.client import Client
from temporalio.worker import Worker

from app.config.logging import configure_logging
from app.config.settings import Settings
from app.database.session import Database
from app.events.store import verify_worker_role
from app.integrations.resolver import TenantIntegrationResolver
from app.knowledge.embeddings import OpenAIEmbedder, open_embedder
from app.knowledge.storage import build_storage
from app.knowledge.worker import KnowledgeWorker
from app.workflows.activities import WorkflowActivities
from app.workflows.definitions import (
    AppointmentReminderWorkflow,
    DocumentIngestionWorkflow,
    PostCallWorkflow,
    TenantOnboardingWorkflow,
)
from app.workflows.dispatch import WorkflowDispatcher
from app.workflows.store import WorkflowIntentStore

logger = logging.getLogger(__name__)


async def run(settings: Settings, *, once: bool = False, check: bool = False) -> None:
    if not settings.temporal_enabled or settings.worker_database_url is None:
        raise ValueError("Temporal worker requires enabled Temporal and worker credentials")
    database = Database(settings.model_copy(update={"database_url": settings.worker_database_url}))
    per_tenant = settings.tenant_integrations_enabled
    embedder = OpenAIEmbedder(settings) if settings.knowledge_enabled and not per_tenant else None
    resolver = TenantIntegrationResolver(
        database.session_factory, settings, role_check=verify_worker_role
    )
    knowledge_worker = (
        KnowledgeWorker(
            database.session_factory,
            build_storage(settings),
            embedder,
            tenant_embedder=(
                (lambda tenant_id: open_embedder(settings, resolver, tenant_id))
                if per_tenant
                else None
            ),
        )
        if settings.knowledge_enabled
        else None
    )
    stop = asyncio.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    try:
        async with database.session_factory.begin() as session:
            await verify_worker_role(session)
        client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
            api_key=settings.temporal_api_key.get_secret_value() or None,
            tls=settings.temporal_tls,
        )
        store = WorkflowIntentStore(database.session_factory)
        dispatcher = WorkflowDispatcher(store, client, settings.temporal_task_queue)
        if check:
            return
        if once:
            await dispatcher.process_one()
            return
        activities = WorkflowActivities(database.session_factory, knowledge_worker)
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[
                DocumentIngestionWorkflow,
                PostCallWorkflow,
                TenantOnboardingWorkflow,
                AppointmentReminderWorkflow,
            ],
            activities=[
                activities.process_document_job,
                activities.record_post_call,
                activities.record_tenant_onboarding,
                activities.plan_appointment_reminder,
                activities.record_reminder_due,
            ],
        )

        async def dispatch_loop() -> None:
            while not stop.is_set():
                try:
                    processed = await dispatcher.process_one()
                except Exception as exc:
                    logger.warning(
                        "workflow_dispatch_unavailable", extra={"error_type": type(exc).__name__}
                    )
                    processed = False
                if not processed:
                    try:
                        await asyncio.wait_for(stop.wait(), settings.temporal_dispatch_poll_seconds)
                    except TimeoutError:
                        continue

        async with worker:
            dispatcher_task = asyncio.create_task(dispatch_loop())
            try:
                await stop.wait()
            finally:
                dispatcher_task.cancel()
                await asyncio.gather(dispatcher_task, return_exceptions=True)
    finally:
        if embedder is not None:
            await embedder.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_environment()
    configure_logging(settings.log_level)
    try:
        asyncio.run(run(settings, once=args.once, check=args.check))
    except Exception as exc:
        logger.error("workflow_worker_failed", extra={"error_type": type(exc).__name__})
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
