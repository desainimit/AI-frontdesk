"""Runs committed job intents in the worker process, one claim at a time."""

import logging

from app.workflows.activities import WorkflowActivities
from app.workflows.store import ClaimedIntent, WorkflowIntentStore

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(
        self, store: WorkflowIntentStore, activities: WorkflowActivities, *, max_attempts: int = 8
    ) -> None:
        self.store, self.activities, self.max_attempts = store, activities, max_attempts

    async def _run(self, intent: ClaimedIntent) -> str:
        if intent.kind == "post_call":
            return await self.activities.record_post_call(intent.request)
        raise ValueError("Unsupported job kind")

    async def process_one(self) -> bool:
        """Claim and run one intent. False means the queue is empty right now."""
        intent = await self.store.claim()
        if intent is None:
            return False
        try:
            outcome = await self._run(intent)
        except Exception as exc:
            # Exception text can contain caller data; only the type is logged.
            logger.warning(
                "job_failed",
                extra={"event_id": str(intent.id), "error_type": type(exc).__name__},
            )
            outcome = "failed"
        if outcome == "recorded":
            finished = await self.store.finish(intent)
        else:
            # "pending" is not a fault, but it still must not wait forever.
            finished = await self.store.finish(
                intent, error=outcome, dead=intent.attempts >= self.max_attempts
            )
        if not finished:
            logger.warning("job_lease_lost", extra={"event_id": str(intent.id)})
        return True
