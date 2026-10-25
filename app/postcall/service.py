"""Tenant-authorized post-call reporting and derived daily analytics."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditAction
from app.audit.repository import AuditRepository
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.models import Call
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import NotFoundError
from app.postcall.models import CallTranscript, PostCallReport
from app.postcall.schemas import (
    DailyCallAnalytics,
    PostCallInput,
    PostCallReportView,
    TranscriptLine,
)


class PostCallQueryService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def _scope(self, session: AsyncSession, context: RequestContext) -> None:
        await verify_runtime_role(session)
        await set_tenant_scope(session, context.tenant_id)
        if context.actor_type == ActorType.PLATFORM_SUPPORT:
            from app.platform.support import validate_support_context

            await validate_support_context(session, context)

    async def report(self, context: RequestContext, call_id: UUID) -> PostCallReportView:
        require_permission(context, Permission.CALL_READ)
        async with self.sessions.begin() as session:
            await self._scope(session, context)
            report = await session.scalar(
                select(PostCallReport).where(
                    PostCallReport.tenant_id == context.tenant_id,
                    PostCallReport.call_id == call_id,
                )
            )
            if report is None:
                raise NotFoundError()
            AuditService(AuditRepository(session, context)).record(
                context, AuditAction.POST_CALL_REPORT_VIEWED, "post_call_report", report.id
            )
            return PostCallReportView.model_validate(report)

    async def transcript(self, context: RequestContext, call_id: UUID) -> list[TranscriptLine]:
        """What was said on a finished call, turn by turn. Empty once the clinic's
        retention period has removed it, or when nothing was said."""
        require_permission(context, Permission.CALL_READ)
        async with self.sessions.begin() as session:
            await self._scope(session, context)
            known = await session.scalar(
                select(Call.id).where(Call.tenant_id == context.tenant_id, Call.id == call_id)
            )
            if known is None:
                raise NotFoundError()
            row = await session.scalar(
                select(CallTranscript).where(
                    CallTranscript.tenant_id == context.tenant_id,
                    CallTranscript.call_id == call_id,
                )
            )
            if row is None:
                return []
            AuditService(AuditRepository(session, context)).record(
                context, AuditAction.CALL_TRANSCRIPT_VIEWED, "call_transcript", row.id
            )
            snapshot = PostCallInput.model_validate(row.snapshot)
            return [TranscriptLine(speaker=turn.speaker, text=turn.text) for turn in snapshot.turns]

    async def analytics(
        self, context: RequestContext, start: date, end: date
    ) -> list[DailyCallAnalytics]:
        require_permission(context, Permission.CALL_READ)
        lower = datetime.combine(start, time.min, tzinfo=UTC)
        upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        day = cast(PostCallReport.call_ended_at, Date).label("day")
        completed = func.count().filter(PostCallReport.outcome["call_status"].astext == "completed")
        needs_follow_up = func.count().filter(
            PostCallReport.outcome["follow_up"].astext.is_not(None)
        )
        async with self.sessions.begin() as session:
            await self._scope(session, context)
            rows = (
                await session.execute(
                    select(day, func.count(), completed, needs_follow_up)
                    .where(
                        PostCallReport.tenant_id == context.tenant_id,
                        PostCallReport.call_ended_at >= lower,
                        PostCallReport.call_ended_at < upper,
                    )
                    .group_by(day)
                    .order_by(day)
                )
            ).all()
            return [
                DailyCallAnalytics(
                    day=row[0], total=row[1], completed=row[2], needs_follow_up=row[3]
                )
                for row in rows
            ]
