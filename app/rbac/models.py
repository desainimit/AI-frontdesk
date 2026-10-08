from uuid import UUID

from sqlalchemy import Boolean, ForeignKeyConstraint, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.domain import TenantRecord


class TenantRole(TenantRecord):
    __tablename__ = "tenant_roles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenant_roles_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_tenant_roles_name"),
        UniqueConstraint("tenant_id", "template_key", name="uq_tenant_roles_template"),
    )
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500), default="", server_default="")
    template_key: Mapped[str | None] = mapped_column(String(30))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class RolePermission(TenantRecord):
    __tablename__ = "role_permissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "role_id"], ["tenant_roles.tenant_id", "tenant_roles.id"]
        ),
        UniqueConstraint("tenant_id", "role_id", "key", name="uq_role_permissions_key"),
    )
    role_id: Mapped[UUID]
    key: Mapped[str] = mapped_column(String(80))
    allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
