"""Transactional Temporal dispatch intents and tenant-scoped workflow outcomes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_workflows"
down_revision = "0011_verification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_intents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(40)),
        sa.UniqueConstraint(
            "tenant_id", "kind", "resource_id", "generation", name="uq_workflow_intent"
        ),
        sa.CheckConstraint(
            "kind IN ('document_ingestion','post_call','tenant_onboarding','appointment_reminder')",
            name="kind_allowed",
        ),
        sa.CheckConstraint(
            "status IN ('pending','dispatching','dispatched','dead')", name="status_allowed"
        ),
        sa.CheckConstraint("generation >= 1 AND attempts >= 0", name="nonnegative_counts"),
    )
    op.create_index(
        "ix_workflow_intents_dispatch", "workflow_intents", ["status", "available_at", "created_at"]
    )
    op.create_table(
        "workflow_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("outcome", JSONB(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "kind", "resource_id", "generation", name="uq_workflow_result"
        ),
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for table in ("workflow_intents", "workflow_results"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    op.drop_table("workflow_results")
    op.drop_index("ix_workflow_intents_dispatch", "workflow_intents")
    op.drop_table("workflow_intents")
