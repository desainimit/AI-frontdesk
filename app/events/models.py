from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin


class IdempotencyRecord(IdentityMixin, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["users.tenant_id", "users.id"]),
        UniqueConstraint("tenant_id", "actor_id", "key_digest", name="uq_idempotency_actor_key"),
        ForeignKeyConstraint(
            ["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"], name="fk_idempotency_call"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "support_session_id"],
            ["support_sessions.tenant_id", "support_sessions.id"],
            name="fk_idempotency_support_session",
        ),
        UniqueConstraint("tenant_id", "call_id", "key_digest", name="uq_idempotency_call_key"),
        UniqueConstraint(
            "tenant_id", "support_session_id", "key_digest", name="uq_idempotency_support_key"
        ),
        CheckConstraint(
            "(actor_id IS NOT NULL AND call_id IS NULL AND support_session_id IS NULL "
            "AND platform_user_id IS NULL) OR "
            "(call_id IS NOT NULL AND actor_id IS NULL AND support_session_id IS NULL "
            "AND platform_user_id IS NULL) OR "
            "(support_session_id IS NOT NULL AND platform_user_id IS NOT NULL "
            "AND actor_id IS NULL AND call_id IS NULL)",
            name="idempotency_principal_exactly_one",
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_id: Mapped[UUID | None]
    call_id: Mapped[UUID | None]
    platform_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("platform_users.id"))
    support_session_id: Mapped[UUID | None]
    key_digest: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, object]] = mapped_column(JSONB)


class OutboxEvent(IdentityMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "aggregate_id"], ["appointments.tenant_id", "appointments.id"]
        ),
        UniqueConstraint(
            "tenant_id", "aggregate_id", "aggregate_version", name="uq_outbox_aggregate_version"
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'delivered', 'dead')", name="status_allowed"
        ),
        CheckConstraint(
            "attempts >= 0 AND aggregate_version >= 1 AND schema_version >= 1",
            name="positive_versions",
        ),
        CheckConstraint(
            "(status = 'processing') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="lease_required",
        ),
        Index("ix_outbox_delivery", "status", "available_at", "created_at"),
    )
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    aggregate_id: Mapped[UUID]
    aggregate_version: Mapped[int]
    event_type: Mapped[str] = mapped_column(String(80))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    request_id: Mapped[UUID]
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(80))
