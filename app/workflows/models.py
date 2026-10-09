"""Small transactional job intents claimed and run by the worker."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin

WORKFLOW_KINDS = ("post_call",)


class WorkflowIntent(IdentityMixin, Base):
    __tablename__ = "workflow_intents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "kind", "resource_id", "generation", name="uq_workflow_intent"
        ),
        CheckConstraint("kind IN ('post_call')", name="kind_allowed"),
        CheckConstraint(
            "status IN ('pending','dispatching','dispatched','dead')", name="status_allowed"
        ),
        CheckConstraint("generation >= 1 AND attempts >= 0", name="nonnegative_counts"),
        Index("ix_workflow_intents_dispatch", "status", "available_at", "created_at"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(40))
