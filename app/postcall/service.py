"""Tenant-authorized post-call reporting and derived daily analytics."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from sqlalchemy import Date, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditAction
from app.audit.repository import AuditRepository
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import NotFoundError
from app.postcall.models import FollowUpEvent, PostCallReport
from app.postcall.schemas import DailyCallAnalytics, FollowUpView, PostCallReportView


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

    async def follow_ups(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        status: str | None = "pending",
        kind: str | None = None,
    ) -> list[FollowUpView]:
        require_permission(context, Permission.CALL_READ)
        async with self.sessions.begin() as session:
            await self._scope(session, context)
            query = select(FollowUpEvent).where(FollowUpEvent.tenant_id == context.tenant_id)
            if status is not None:
                query = query.where(FollowUpEvent.status == status)
            if kind is not None:
                query = query.where(FollowUpEvent.kind == kind)
            rows = (
                await session.scalars(
                    query.order_by(FollowUpEvent.created_at, FollowUpEvent.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [FollowUpView.model_validate(row) for row in rows]

    async def acknowledge(self, context: RequestContext, event_id: UUID) -> FollowUpView:
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        async with self.sessions.begin() as session:
            await self._scope(session, context)
            row = await session.scalar(
                update(FollowUpEvent)
                .where(
                    FollowUpEvent.tenant_id == context.tenant_id,
                    FollowUpEvent.id == event_id,
                    FollowUpEvent.status == "pending",
                )
                .values(
                    status="acknowledged",
                    acknowledged_at=func.coalesce(FollowUpEvent.acknowledged_at, func.now()),
                )
                .returning(FollowUpEvent)
            )
            if row is None:
                row = await session.scalar(
                    select(FollowUpEvent).where(
                        FollowUpEvent.tenant_id == context.tenant_id,
                        FollowUpEvent.id == event_id,
                    )
                )
                if row is None:
                    raise NotFoundError()
            else:
                AuditService(AuditRepository(session, context)).record(
                    context, AuditAction.FOLLOW_UP_ACKNOWLEDGED, "follow_up_event", event_id
                )
            return FollowUpView.model_validate(row)

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
