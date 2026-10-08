"""Lease-based claiming of committed job intents by the worker."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events.store import verify_worker_role
from app.workflows.intent import WorkflowKind, WorkflowRequest
from app.workflows.models import WORKFLOW_KINDS, WorkflowIntent


@dataclass(frozen=True)
class ClaimedIntent:
    id: UUID
    kind: WorkflowKind
    request: WorkflowRequest
    lease_token: UUID
    attempts: int


class WorkflowIntentStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def claim(self) -> ClaimedIntent | None:
        async with self._sessions.begin() as session:
            await verify_worker_role(session)
            row = await session.scalar(
                select(WorkflowIntent)
                .where(
                    or_(
                        and_(
                            WorkflowIntent.status == "pending",
                            WorkflowIntent.available_at <= func.now(),
                        ),
                        and_(
                            WorkflowIntent.status == "dispatching",
                            WorkflowIntent.lease_until <= func.now(),
                        ),
                    )
                )
                .order_by(WorkflowIntent.available_at, WorkflowIntent.created_at, WorkflowIntent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            token = uuid4()
            row.status = "dispatching"
            row.lease_token = token
            row.lease_until = await session.scalar(select(func.now() + timedelta(seconds=60)))
            row.attempts += 1
            if row.kind not in WORKFLOW_KINDS:
                # Retire it here: raising would roll back and reclaim the same row forever.
                row.status, row.last_error = "dead", "unsupported_kind"
                row.lease_token, row.lease_until = None, None
                return None
            return ClaimedIntent(
                id=row.id,
                kind=row.kind,  # type: ignore[arg-type]
                request=WorkflowRequest(str(row.tenant_id), str(row.resource_id), row.generation),
                lease_token=token,
                attempts=row.attempts,
            )

    async def finish(
        self, intent: ClaimedIntent, *, error: str | None = None, dead: bool = False
    ) -> bool:
        """Complete the claim, or with ``error`` retry later; ``dead`` stops retrying."""
        async with self._sessions.begin() as session:
            if dead:
                values = {
                    "status": "dead",
                    "last_error": error,
                    "lease_token": None,
                    "lease_until": None,
                }
            elif error is None:
                values = {
                    "status": "dispatched",
                    "dispatched_at": func.now(),
                    "last_error": None,
                    "lease_token": None,
                    "lease_until": None,
                }
            else:
                backoff = min(300, 2 ** min(intent.attempts, 8))
                values = {
                    "status": "pending",
                    "available_at": func.now() + timedelta(seconds=backoff),
                    "last_error": error,
                    "lease_token": None,
                    "lease_until": None,
                }
            updated = await session.scalar(
                update(WorkflowIntent)
                .where(
                    WorkflowIntent.id == intent.id,
                    WorkflowIntent.status == "dispatching",
                    WorkflowIntent.lease_token == intent.lease_token,
                    WorkflowIntent.lease_until > func.now(),
                )
                .values(**values)
                .returning(WorkflowIntent.id)
            )
            return updated is not None
