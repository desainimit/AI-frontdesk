"""Financial projections of admitted work; provider records remain in their own domain."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.domain import TenantRecord


class UsageReservation(TenantRecord):
    __tablename__ = "usage_reservations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "resource_key", name="uq_usage_reservations_resource"),
        UniqueConstraint("tenant_id", "id", name="uq_usage_reservations_tenant_id_id"),
        Index("ix_usage_reservations_period", "tenant_id", "period_start", "meter", "state"),
        Index(
            "ix_usage_reservations_expiry",
            "lease_expires_at",
            "tenant_id",
            postgresql_where=text("state IN ('held', 'running')"),
        ),
        Index(
            "ix_usage_reservations_active",
            "tenant_id",
            "meter",
            "lease_expires_at",
            postgresql_where=text("state IN ('held', 'running', 'reconciliation')"),
        ),
        Index(
            "ix_usage_reservations_spending",
            "tenant_id",
            "settled_at",
            postgresql_where=text("state = 'settled'"),
        ),
        CheckConstraint(
            "meter IN ('voice_seconds', 'sms_messages', 'email_messages', 'chat_messages')",
            name="meter",
        ),
        CheckConstraint(
            "state IN ('held', 'running', 'reconciliation', 'settled', 'released')",
            name="state",
        ),
        CheckConstraint(
            "revision >= 1 AND max_units BETWEEN 1 AND 100000000 AND "
            "price_minor BETWEEN 0 AND 1000000 AND quoted_minor = max_units * price_minor "
            "AND quoted_minor <= 1000000000000 AND period_end > period_start "
            "AND configuration_version >= 1",
            name="quote",
        ),
        CheckConstraint(
            "(state IN ('held', 'running', 'reconciliation') AND actual_units IS NULL "
            "AND settled_at IS NULL AND outcome_reference IS NULL) OR "
            "(state = 'settled' AND actual_units BETWEEN 0 AND max_units "
            "AND settled_at IS NOT NULL AND outcome_reference IS NOT NULL) OR "
            "(state = 'released' AND actual_units = 0 AND settled_at IS NOT NULL "
            "AND outcome_reference IS NOT NULL)",
            name="outcome",
        ),
        CheckConstraint(
            "(state = 'held' AND fence IS NULL) OR (state IN ('running', 'reconciliation', "
            "'settled') AND fence IS NOT NULL) OR state = 'released'",
            name="fence",
        ),
    )
    resource_key: Mapped[UUID]
    revision: Mapped[int]
    meter: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(20))
    plan_version_id: Mapped[UUID] = mapped_column(ForeignKey("billing_plan_versions.id"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    configuration_version: Mapped[int]
    max_units: Mapped[int] = mapped_column(BigInteger)
    price_minor: Mapped[int] = mapped_column(BigInteger)
    quoted_minor: Mapped[int] = mapped_column(BigInteger)
    actual_units: Mapped[int | None] = mapped_column(BigInteger)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fence: Mapped[UUID | None]
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_reference: Mapped[str | None] = mapped_column(String(200))


class UsageCommand(TenantRecord):
    __tablename__ = "usage_commands"
    __table_args__ = (
        UniqueConstraint("tenant_id", "mutation_id", name="uq_usage_commands_mutation"),
        Index(
            "ix_usage_commands_reservation",
            "tenant_id",
            text("((result -> 'reservation'::text) ->> 'id'::text)"),
        ),
    )
    mutation_id: Mapped[UUID]
    command_hash: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[UUID]
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
