from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    CALLER = "caller"
    PLATFORM_SUPPORT = "platform_support"


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


@dataclass(frozen=True)
class RequestContext:
    request_id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    actor_type: ActorType
    role: Role | None = None
    call_id: UUID | None = None
    patient_id: UUID | None = None
    provider_call_id: str | None = None
    # A platform administrator working inside this workspace: their account.
    platform_user_id: UUID | None = None
    support_session_id: UUID | None = None
    support_expires_at: datetime | None = None
    capabilities: frozenset[str] = frozenset()
    # The signed-in session behind this request; rechecked inside every transaction.
    session_id: UUID | None = None
    role_id: UUID | None = None
    authorization_revision: int = 0
    role_name: str | None = None

    def __post_init__(self) -> None:
        if self.actor_type == ActorType.CALLER and (
            self.call_id is None
            or self.provider_call_id is None
            or self.actor_id is not None
            or self.role is not None
        ):
            raise ValueError("Caller context requires a server call binding")
        if self.actor_type == ActorType.USER and (self.actor_id is None or self.role is None):
            raise ValueError("User context requires an actor and role")
        if self.actor_type == ActorType.PLATFORM_SUPPORT and (
            self.actor_id is not None
            or self.role is not None
            or self.platform_user_id is None
            or self.session_id is None
            or self.support_session_id is None
            or self.support_expires_at is None
            or not self.capabilities
        ):
            raise ValueError("Support context requires a platform actor and scoped session")
