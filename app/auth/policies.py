from enum import StrEnum
from uuid import UUID

from app.auth.context import ActorType, RequestContext
from app.core.errors import AuthorizationError


class Permission(StrEnum):
    TENANT_READ = "tenant:read"
    TENANT_ACTIVATE = "tenant:activate"
    SETTINGS_READ = "settings:read"
    SETTINGS_UPDATE = "settings:update"
    DIRECTORY_READ = "directory:read"
    DIRECTORY_WRITE = "directory:write"
    PATIENT_READ = "patient:read"
    PATIENT_WRITE = "patient:write"
    APPOINTMENT_READ = "appointment:read"
    APPOINTMENT_WRITE = "appointment:write"
    CALL_READ = "call:read"
    FOLLOW_UP_MANAGE = "follow_up:manage"
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    USER_READ = "user:read"
    USER_INVITE = "user:invite"
    USER_MANAGE = "user:manage"
    ROLE_READ = "role:read"
    ROLE_CREATE = "role:create"
    ROLE_UPDATE = "role:update"
    ROLE_DELETE = "role:delete"
    ROLE_ASSIGN = "role:assign"
    INTEGRATION_READ = "integration:read"
    INTEGRATION_MANAGE = "integration:manage"
    AUDIT_READ = "audit:read"


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
