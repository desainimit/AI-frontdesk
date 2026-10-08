from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import IdentityMixin, TimestampMixin


class User(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        CheckConstraint("role IN ('admin', 'member')", name="role_allowed"),
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["tenant_roles.tenant_id", "tenant_roles.id"],
            name="fk_users_tenant_role",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20), default="member")
    role_id: Mapped[UUID | None]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
