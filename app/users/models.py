from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin, TimestampMixin


class User(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id_id"),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_id_email"),
        UniqueConstraint("tenant_id", "account_id", name="uq_users_tenant_id_account_id"),
        ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["tenant_roles.tenant_id", "tenant_roles.id"],
            name="fk_users_tenant_role",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    # The person behind this membership; they sign in with the account's email and password.
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    email: Mapped[str] = mapped_column(String(254))
    display_name: Mapped[str] = mapped_column(String(200))
    # What this member may do here; see app.rbac.
    role_id: Mapped[UUID]
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Empty while the invitation is unanswered.
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
