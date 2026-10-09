from datetime import datetime

from app.audit.events import AuditAction
from app.audit.schemas import AuditView
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.unit_of_work import UnitOfWork


class AuditQueryService:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._uow = unit_of_work

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        action: str | None = None,
        actor_type: str | None = None,
        resource_type: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> list[AuditView]:
        require_permission(context, Permission.AUDIT_READ)
        async with self._uow.transaction(context) as transaction:
            events = await transaction.audit.list(
                limit,
                offset,
                action=action,
                actor_type=actor_type,
                resource_type=resource_type,
                created_from=created_from,
                created_to=created_to,
            )
            AuditService(transaction.audit).record(
                context, AuditAction.AUDIT_VIEWED, "audit_events"
            )
            return [AuditView.model_validate(event) for event in events]
