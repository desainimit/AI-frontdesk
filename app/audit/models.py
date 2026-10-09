from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin


class AuditEvent(IdentityMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["users.tenant_id", "users.id"]),
        CheckConstraint(
            "actor_type IN ('user', 'system', 'platform_support')", name="actor_type_allowed"
        ),
        CheckConstraint("actor_type <> 'user' OR actor_id IS NOT NULL", name="user_actor_required"),
        CheckConstraint(
            "actor_type <> 'platform_support' OR "
            "(actor_id IS NULL AND platform_user_id IS NOT NULL "
            "AND support_session_id IS NOT NULL)",
            name="support_actor_required",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID | None]
    platform_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))
    support_session_id: Mapped[UUID | None]
    actor_type: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[UUID | None]
    request_id: Mapped[UUID]
    call_id: Mapped[UUID | None]
