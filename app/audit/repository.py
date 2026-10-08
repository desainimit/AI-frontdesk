from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditEvent
from app.auth.context import RequestContext
from app.auth.policies import require_tenant


class AuditRepository:
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session = session
        self._context = context

    def append(self, event: AuditEvent) -> None:
        require_tenant(self._context, event.tenant_id)
        self._session.add(event)

    async def list(
        self,
        limit: int,
        offset: int,
        *,
        action: str | None = None,
        actor_type: str | None = None,
        resource_type: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> Sequence[AuditEvent]:
        query = select(AuditEvent).where(AuditEvent.tenant_id == self._context.tenant_id)
        if action is not None:
            query = query.where(AuditEvent.action == action)
        if actor_type is not None:
            query = query.where(AuditEvent.actor_type == actor_type)
        if resource_type is not None:
            query = query.where(AuditEvent.resource_type == resource_type)
        if created_from is not None:
            query = query.where(AuditEvent.created_at >= created_from)
        if created_to is not None:
            query = query.where(AuditEvent.created_at <= created_to)
        return (
            await self._session.scalars(
                query.order_by(AuditEvent.created_at.desc(), AuditEvent.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
