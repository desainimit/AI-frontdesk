from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import IdentityMixin, TimestampMixin


class SetupProgress(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "tenant_setup_progress"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_setup_progress_tenant_id"),
        CheckConstraint("revision >= 1", name="positive_revision"),
        CheckConstraint("jsonb_typeof(reviewed) = 'object'", name="reviewed_object"),
        CheckConstraint(
            "current_step IN ('business', 'administrator', 'team', 'agent', "
            "'knowledge', 'channels', 'billing', 'tests', 'publication')",
            name="step_allowed",
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    revision: Mapped[int] = mapped_column(default=1)
    current_step: Mapped[str] = mapped_column(String(32), default="business")
    reviewed: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict[str, str])
