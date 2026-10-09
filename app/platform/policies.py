from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.core.errors import AuthorizationError


class PlatformRole(StrEnum):
    SUPER_ADMIN = "super_admin"


class PlatformPermission(StrEnum):
    IDENTITY_READ = "platform:identity:read"
    TENANT_READ = "platform:tenant:read"
    TENANT_CREATE = "platform:tenant:create"
    TENANT_UPDATE = "platform:tenant:update"
    SUPPORT_ENTER = "platform:support:enter"
    SUPPORT_REVOKE = "platform:support:revoke"
    SUPPORT_AUDIT_READ = "platform:support:audit:read"


@dataclass(frozen=True)
class PlatformContext:
    user_id: UUID
    session_id: UUID
    request_id: UUID
    role: PlatformRole
    mfa_verified: bool = False


def require_platform_permission(context: PlatformContext, permission: PlatformPermission) -> None:
    if context.role != PlatformRole.SUPER_ADMIN or permission not in frozenset(PlatformPermission):
        raise AuthorizationError()
    if permission != PlatformPermission.IDENTITY_READ and not context.mfa_verified:
        raise AuthorizationError()
