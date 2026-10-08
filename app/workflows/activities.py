"""Database and document effects executed outside Temporal workflow replay."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from app.appointments.models import Appointment
from app.calls.models import Call
from app.database.rls import set_tenant_scope
from app.events.store import verify_worker_role
from app.knowledge.models import KnowledgeJob
from app.knowledge.worker import KnowledgeWorker
from app.postcall.models import CallTranscript, FollowUpEvent, PostCallReport
from app.postcall.schemas import PostCallInput, analyze_post_call
from app.tenants.models import Tenant
from app.workflows.intent import WorkflowRequest
from app.workflows.models import WorkflowResult


class WorkflowActivities:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        knowledge_worker: KnowledgeWorker | None = None,
    ) -> None:
        self.sessions, self.knowledge_worker = sessions, knowledge_worker

    @staticmethod
    async def _record(
        session: AsyncSession,
        request: WorkflowRequest,
        kind: str,
        outcome: dict[str, Any],
    ) -> None:
        await session.execute(
            insert(WorkflowResult)
            .values(
                tenant_id=UUID(request.tenant_id),
                kind=kind,
                resource_id=UUID(request.resource_id),
                generation=request.generation,
                outcome=outcome,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "kind", "resource_id", "generation"]
            )
        )

    @activity.defn(name="process_document_job")
    async def process_document_job(self, request: WorkflowRequest) -> str:
        if self.knowledge_worker is None:
            raise RuntimeError("Knowledge ingestion is unavailable")
        tenant_id, version_id = UUID(request.tenant_id), UUID(request.resource_id)
        async with self.sessions.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, tenant_id)
            job = await session.scalar(
                select(KnowledgeJob).where(
                    KnowledgeJob.tenant_id == tenant_id,
                    KnowledgeJob.document_version_id == version_id,
                )
            )
            if job is None:
                return "dead"
            if job.status in {"delivered", "dead"}:
                return job.status
        await self.knowledge_worker.process_one(version_id, tenant_id)
        async with self.sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            job = await session.scalar(
                select(KnowledgeJob).where(
                    KnowledgeJob.tenant_id == tenant_id,
                    KnowledgeJob.document_version_id == version_id,
                )
            )
            return job.status if job is not None else "dead"

    @activity.defn(name="record_post_call")
    async def record_post_call(self, request: WorkflowRequest) -> str:
        tenant_id, call_id = UUID(request.tenant_id), UUID(request.resource_id)
        async with self.sessions.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, tenant_id)
            call = (
                await session.execute(
                    select(
                        Call.status,
                        Call.ended_at,
                        Call.started_at,
                        Call.created_at,
                        Call.received_frames,
                        Call.sent_frames,
                    ).where(Call.tenant_id == tenant_id, Call.id == call_id)
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
            await self._record(
                session,
                request,
                "post_call",
                {
                    "status": call.status,
                    "duration_seconds": max(
                        0,
                        int((call.ended_at - (call.started_at or call.created_at)).total_seconds()),
                    ),
                    "received_frames": call.received_frames,
                    "sent_frames": call.sent_frames,
                    "turn_count": analysis.turn_count,
                    "resolution": analysis.outcome.resolution,
                    "follow_up": analysis.outcome.follow_up,
                },
            )
            return "recorded"

    @activity.defn(name="record_tenant_onboarding")
    async def record_tenant_onboarding(self, request: WorkflowRequest) -> str:
        tenant_id = UUID(request.tenant_id)
        if request.resource_id != request.tenant_id:
            raise ValueError("Tenant onboarding resource mismatch")
        async with self.sessions.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, tenant_id)
            tenant = (
                await session.execute(
                    select(
                        Tenant.is_active,
                        Tenant.language_policy,
                        Tenant.verification_policy,
                    ).where(Tenant.id == tenant_id)
                )
            ).one_or_none()
            if tenant is None:
                raise ValueError("Tenant unavailable")
            await self._record(
                session,
                request,
                "tenant_onboarding",
                {
                    "active": tenant.is_active,
                    "language_policy_configured": bool(tenant.language_policy),
                    "verification_policy_configured": bool(tenant.verification_policy),
                },
            )
            return "recorded"

    @activity.defn(name="plan_appointment_reminder")
    async def plan_appointment_reminder(self, request: WorkflowRequest) -> str | None:
        tenant_id, appointment_id = UUID(request.tenant_id), UUID(request.resource_id)
        async with self.sessions.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, tenant_id)
            appointment = (
                await session.execute(
                    select(Appointment.starts_at).where(
                        Appointment.tenant_id == tenant_id,
                        Appointment.id == appointment_id,
                        Appointment.version == request.generation,
                        Appointment.status == "booked",
                    )
                )
            ).one_or_none()
            if appointment is None:
                return None
            now = datetime.now(UTC)
            if appointment.starts_at <= now + timedelta(minutes=5):
                return None
            return max(now, appointment.starts_at - timedelta(hours=1)).isoformat()

    @activity.defn(name="record_reminder_due")
    async def record_reminder_due(self, request: WorkflowRequest) -> str:
        tenant_id, appointment_id = UUID(request.tenant_id), UUID(request.resource_id)
        async with self.sessions.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, tenant_id)
            appointment = (
                await session.execute(
                    select(Appointment.starts_at).where(
                        Appointment.tenant_id == tenant_id,
                        Appointment.id == appointment_id,
                        Appointment.version == request.generation,
                        Appointment.status == "booked",
                    )
                )
            ).one_or_none()
            if appointment is None or appointment.starts_at <= datetime.now(UTC):
                return "skipped"
            await self._record(
                session,
                request,
                "appointment_reminder",
                {"state": "due", "starts_at": appointment.starts_at.isoformat()},
            )
            return "due"
