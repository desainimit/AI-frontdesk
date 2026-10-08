"""Safe onboarding metadata; never provider secrets or invitation bearer tokens."""

from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import IdentityMixin, TimestampMixin


class TenantOnboarding(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "tenant_onboardings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_onboardings_tenant_id"),
        UniqueConstraint("creator_id", "key_digest", name="uq_tenant_onboardings_creation_key"),
        ForeignKeyConstraint(["tenant_id", "admin_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(["tenant_id", "location_id"], ["locations.tenant_id", "locations.id"]),
        CheckConstraint("revision >= 1", name="positive_revision"),
        CheckConstraint("status IN ('draft', 'invited')", name="onboarding_status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    admin_id: Mapped[UUID]
    location_id: Mapped[UUID]
    creator_id: Mapped[UUID] = mapped_column(ForeignKey("platform_users.id"))
    key_digest: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    draft: Mapped[dict[str, Any]] = mapped_column(JSONB)
