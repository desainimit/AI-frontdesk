"""Tenant idempotency records and durable outbox delivery.

Revision ID: 0005_reliability
Revises: 0004_platform_admin
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_reliability"
down_revision = "0004_platform_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity() -> list[sa.Column[UUID] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_unique_constraint("uq_appointments_tenant_id_id", "appointments", ["tenant_id", "id"])
    op.create_table(
        "idempotency_records",
        *_identity(),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "actor_id"], ["users.tenant_id", "users.id"]),
        sa.UniqueConstraint("tenant_id", "actor_id", "key_digest", name="uq_idempotency_actor_key"),
    )
    op.create_table(
        "outbox_events",
        *_identity(),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(80), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "aggregate_id"], ["appointments.tenant_id", "appointments.id"]
        ),
        sa.UniqueConstraint(
            "tenant_id", "aggregate_id", "aggregate_version", name="uq_outbox_aggregate_version"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'delivered', 'dead')", name="status_allowed"
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND aggregate_version >= 1 AND schema_version >= 1",
            name="positive_versions",
        ),
        sa.CheckConstraint(
            "(status = 'processing') = (lease_token IS NOT NULL AND lease_until IS NOT NULL)",
            name="lease_required",
        ),
    )
    op.create_index("ix_outbox_delivery", "outbox_events", ["status", "available_at", "created_at"])
    for table in ("idempotency_records", "outbox_events"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("idempotency_records")
    op.drop_constraint("uq_appointments_tenant_id_id", "appointments", type_="unique")
