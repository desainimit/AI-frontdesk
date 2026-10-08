from enum import StrEnum
from uuid import UUID

from app.auth.context import ActorType, RequestContext
from app.errors import AuthorizationError


class Permission(StrEnum):
    CALL_READ = "call:read"
    FOLLOW_UP_MANAGE = "follow_up:manage"
    TENANT_READ = "tenant:read"
    TENANT_UPDATE = "tenant:update"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    USER_READ = "user:read"
    USER_INVITE = "user:invite"
    USER_MANAGE = "user:manage"
    INTEGRATION_READ = "integration:read"
    INTEGRATION_MANAGE = "integration:manage"
    TENANT_ACTIVATE = "tenant:activate"
    AUDIT_READ = "audit:read"
    DIRECTORY_READ = "directory:read"
    DIRECTORY_WRITE = "directory:write"
    PATIENT_READ = "patient:read"
    PATIENT_WRITE = "patient:write"
    APPOINTMENT_READ = "appointment:read"
    APPOINTMENT_WRITE = "appointment:write"
    ROLE_READ = "role:read"
    ROLE_CREATE = "role:create"
    ROLE_UPDATE = "role:update"
    ROLE_DELETE = "role:delete"
    ROLE_ASSIGN = "role:assign"
    CONFIGURATION_READ = "configuration:read"
    CONFIGURATION_UPDATE = "configuration:update"
    CONFIGURATION_TEST = "configuration:test"
    CONFIGURATION_PUBLISH = "configuration:publish"
    CONFIGURATION_ROLLBACK = "configuration:rollback"
    AGENT_READ = "agent:read"
    AGENT_UPDATE = "agent:update"
    AGENT_PUBLISH = "agent:publish"
    CHANNEL_READ = "channel:read"
    CHANNEL_UPDATE = "channel:update"
    CHANNEL_PUBLISH = "channel:publish"
    SCHEDULING_READ = "scheduling:read"
    SCHEDULING_UPDATE = "scheduling:update"
    SCHEDULING_PUBLISH = "scheduling:publish"
    PRIVACY_READ = "privacy:read"
    PRIVACY_UPDATE = "privacy:update"
    PRIVACY_PUBLISH = "privacy:publish"
    NOTIFICATION_READ = "notification:read"
    NOTIFICATION_UPDATE = "notification:update"
    NOTIFICATION_PUBLISH = "notification:publish"
    WORKFLOW_PUBLISH = "workflow:publish"
    BILLING_PUBLISH = "billing:publish"
    TENANT_PUBLISH = "tenant:publish"
    BUSINESS_READ = "business:read"
    BUSINESS_WRITE = "business:write"
    CONVERSATION_READ = "conversation:read"
    CONVERSATION_WRITE = "conversation:write"
    TICKET_READ = "ticket:read"
    TICKET_WRITE = "ticket:write"
    WORKFLOW_READ = "workflow:read"
    WORKFLOW_WRITE = "workflow:write"
    BILLING_READ = "billing:read"
    BILLING_MANAGE = "billing:manage"
    PRIVACY_EXPORT = "privacy:export"
    PRIVACY_DELETE = "privacy:delete"


# Product decision (2026-10-07): a platform administrator who enters a workspace may
# read and change everything a tenant administrator can: provider connections,
# activation and the team. Each request is still audited against the support session.
SUPPORT_PERMISSIONS = frozenset(Permission)


def require_permission(context: RequestContext, permission: Permission) -> None:
    if context.actor_type == ActorType.PLATFORM_SUPPORT:
        if permission in SUPPORT_PERMISSIONS and permission.value in context.capabilities:
            return
        raise AuthorizationError()
    if context.actor_type == ActorType.CALLER:
        if permission == Permission.DIRECTORY_READ or (
            context.patient_id is not None
            and permission in {Permission.APPOINTMENT_READ, Permission.APPOINTMENT_WRITE}
        ):
            return
        raise AuthorizationError()
    if context.actor_type == ActorType.USER and context.role_id is not None:
        if permission.value in context.capabilities:
            return
        raise AuthorizationError()
    raise AuthorizationError()


def has_permission(context: RequestContext, permission: Permission) -> bool:
    """Non-raising check, for optional detail such as a related record's name."""
    try:
        require_permission(context, permission)
    except AuthorizationError:
        return False
    return True


def require_tenant(context: RequestContext, tenant_id: UUID) -> None:
    if context.tenant_id != tenant_id:
        raise AuthorizationError()
