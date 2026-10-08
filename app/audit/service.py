from uuid import UUID

from app.audit.events import AuditAction
from app.audit.models import AuditEvent
from app.audit.repository import AuditRepository
from app.auth.context import ActorType, RequestContext


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    def record(
        self,
        context: RequestContext,
        action: AuditAction,
        resource_type: str,
        resource_id: UUID | None = None,
    ) -> None:
        self._repository.append(
            AuditEvent(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                platform_user_id=context.platform_user_id,
                support_session_id=context.support_session_id,
                actor_type=ActorType.SYSTEM
                if context.actor_type == ActorType.CALLER
                else context.actor_type,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=context.request_id,
                call_id=context.call_id,
            )
        )
