"""Recoverable handoff from committed PostgreSQL intents to Temporal starts."""

import logging
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from app.workflows.intent import workflow_id
from app.workflows.store import WorkflowIntentStore

logger = logging.getLogger(__name__)

WORKFLOW_NAMES = {
    "document_ingestion": "DocumentIngestionWorkflow",
    "post_call": "PostCallWorkflow",
    "tenant_onboarding": "TenantOnboardingWorkflow",
    "appointment_reminder": "AppointmentReminderWorkflow",
}


class WorkflowDispatcher:
    def __init__(self, store: WorkflowIntentStore, client: Client, task_queue: str) -> None:
        self.store, self.client, self.task_queue = store, client, task_queue

    async def process_one(self) -> bool:
        intent = await self.store.claim()
        if intent is None:
            return False
        try:
            await self.client.start_workflow(
                WORKFLOW_NAMES[intent.kind],
                intent.request,
                id=workflow_id(intent.kind, intent.request),
                task_queue=self.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=timedelta(seconds=10),
            )
        except WorkflowAlreadyStartedError:
            pass
        except Exception as exc:
            logger.warning(
                "temporal_dispatch_retry",
                extra={"intent_id": str(intent.id), "error_type": type(exc).__name__},
            )
            await self.store.finish(intent, error="temporal_unavailable")
            return True
        if not await self.store.finish(intent):
            logger.warning("temporal_dispatch_lease_lost", extra={"intent_id": str(intent.id)})
        return True
