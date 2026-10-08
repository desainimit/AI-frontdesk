from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.domain import TenantRecord
from app.database.types import IdentityMixin


class PlanVersion(IdentityMixin, Base):
    __tablename__ = "billing_plan_versions"
    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_billing_plan_versions_key_version"),
        UniqueConstraint("mutation_id", name="billing_plan_versions_mutation_id_key"),
        CheckConstraint("version >= 1", name="positive_version"),
    )
    key: Mapped[str] = mapped_column(String(64))
    version: Mapped[int]
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    mutation_id: Mapped[UUID]
    command_hash: Mapped[str] = mapped_column(String(64))
    platform_user_id: Mapped[UUID] = mapped_column(ForeignKey("platform_users.id"))
    request_id: Mapped[UUID]


class BillingAccount(TenantRecord):
    __tablename__ = "billing_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_billing_accounts_tenant_id"),
        CheckConstraint(
            (
                "revision >= 0 AND balance_minor BETWEEN 0 AND 1000000000000 AND "
                "reserved_minor BETWEEN 0 AND balance_minor"
            ),
            name="valid_balance",
        ),
        CheckConstraint("currency = 'INR'", name="currency"),
        CheckConstraint(
            "(plan_version_id IS NULL AND period_start IS NULL AND period_end IS NULL) "
            "OR (plan_version_id IS NOT NULL AND period_start IS NOT NULL "
            "AND period_end > period_start)",
            name="subscription_period",
        ),
    )
    revision: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    balance_minor: Mapped[int] = mapped_column(BigInteger)
    reserved_minor: Mapped[int] = mapped_column(BigInteger)
    plan_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("billing_plan_versions.id"))
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingLedger(TenantRecord):
    __tablename__ = "billing_ledger"
    __table_args__ = (
        UniqueConstraint("tenant_id", "revision", name="uq_billing_ledger_tenant_revision"),
        UniqueConstraint("tenant_id", "mutation_id", name="uq_billing_ledger_mutation"),
        CheckConstraint(
            "revision >= 1 AND balance_after_minor BETWEEN 0 AND 1000000000000 "
            "AND reserved_after_minor BETWEEN 0 AND balance_after_minor",
            name="valid_balance",
        ),
        CheckConstraint(
            "kind IN ('subscription', 'adjustment', 'usage', 'hold', 'release')",
            name="kind_allowed",
        ),
        CheckConstraint(
            "(kind = 'adjustment' AND amount_minor <> 0 AND reserved_delta_minor = 0 "
            "AND platform_user_id IS NOT NULL) OR (kind IN ('subscription', 'usage') "
            "AND amount_minor <= 0 AND reserved_delta_minor <= 0) "
            "OR (kind = 'hold' AND amount_minor = 0 AND reserved_delta_minor >= 0) "
            "OR (kind = 'release' AND amount_minor = 0 AND reserved_delta_minor <= 0)",
            name="entry_shape",
        ),
    )
    revision: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))
    mutation_id: Mapped[UUID]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    reserved_delta_minor: Mapped[int] = mapped_column(BigInteger)
    balance_after_minor: Mapped[int] = mapped_column(BigInteger)
    reserved_after_minor: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(500))
    actor_id: Mapped[UUID | None]
    platform_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("platform_users.id"))
    request_id: Mapped[UUID]


class BillingMutation(TenantRecord):
    __tablename__ = "billing_mutations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "mutation_id", name=("uq_billing_mutations_tenant_mutation")),
    )
    mutation_id: Mapped[UUID]
    command_hash: Mapped[str] = mapped_column(String(64))
    actor_key: Mapped[str] = mapped_column(String(100))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
