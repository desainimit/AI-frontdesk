"""Database effects of job intents, run by the worker under its restricted login."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.calls.models import Call
from app.database.rls import set_tenant_scope
from app.events.store import verify_worker_role
from app.postcall.models import CallTranscript, FollowUpEvent, PostCallReport
from app.postcall.schemas import PostCallInput, analyze_post_call
from app.workflows.intent import WorkflowRequest


class WorkflowActivities:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def record_post_call(self, request: WorkflowRequest) -> str:
        """Write the report once. "pending" means the transcript may still arrive."""
        tenant_id, call_id = UUID(request.tenant_id), UUID(request.resource_id)
        async with self.sessions.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, tenant_id)
            call = (
                await session.execute(
                    select(Call.status, Call.ended_at).where(
                        Call.tenant_id == tenant_id, Call.id == call_id
                    )
                )
            ).one_or_none()
            if call is None or call.ended_at is None:
                raise ValueError("Call is not complete")
            raw_snapshot = await session.scalar(
                select(CallTranscript.snapshot).where(
                    CallTranscript.tenant_id == tenant_id, CallTranscript.call_id == call_id
                )
            )
            if raw_snapshot is None and datetime.now(UTC) < call.ended_at + timedelta(seconds=30):
                return "pending"
            snapshot = PostCallInput.model_validate(raw_snapshot) if raw_snapshot else None
            analysis = analyze_post_call(call.status, snapshot)
            inserted = await session.scalar(
                insert(PostCallReport)
                .values(
                    tenant_id=tenant_id,
                    call_id=call_id,
                    call_ended_at=call.ended_at,
                    summary=analysis.summary,
                    outcome=analysis.outcome.model_dump(mode="json"),
                    transcript_digest=analysis.transcript_digest,
                    turn_count=analysis.turn_count,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "call_id"])
                .returning(PostCallReport.id)
            )
            if inserted is not None and analysis.outcome.follow_up is not None:
                session.add(
                    FollowUpEvent(
                        tenant_id=tenant_id,
                        call_id=call_id,
                        kind=analysis.outcome.follow_up,
                        status="pending",
                    )
                )
            return "recorded"
