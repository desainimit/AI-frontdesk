"""Restricted post-call snapshots, reports, and follow-up events."""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013_post_call"
down_revision = "0012_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _base() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "call_transcripts",
        *_base(),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"]),
        sa.UniqueConstraint("tenant_id", "call_id", name="uq_call_transcript"),
    )
    op.create_table(
        "post_call_reports",
        *_base(),
        sa.Column("call_ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.String(1200), nullable=False),
        sa.Column("outcome", JSONB(), nullable=False),
        sa.Column("transcript_digest", sa.String(64), nullable=False),
        sa.Column("turn_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"]),
        sa.UniqueConstraint("tenant_id", "call_id", name="uq_post_call_report"),
        sa.CheckConstraint("turn_count BETWEEN 0 AND 80", name="turn_count_bounded"),
    )
    op.create_index(
        "ix_post_call_reports_tenant_ended",
        "post_call_reports",
        ["tenant_id", "call_ended_at"],
    )
    op.create_table(
        "follow_up_events",
        *_base(),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"]),
        sa.UniqueConstraint("tenant_id", "call_id", "kind", name="uq_follow_up_call_kind"),
        sa.CheckConstraint("kind IN ('human_review','appointment_followup')", name="kind_allowed"),
        sa.CheckConstraint("status IN ('pending','acknowledged')", name="status_allowed"),
    )
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for table in ("call_transcripts", "post_call_reports", "follow_up_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    op.drop_table("follow_up_events")
    op.drop_index("ix_post_call_reports_tenant_ended", "post_call_reports")
    op.drop_table("post_call_reports")
    op.drop_table("call_transcripts")
