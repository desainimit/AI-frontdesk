"""Tenant phone routing and inbound call lifecycle."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0006_telephony"
down_revision = "0005_reliability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "phone_numbers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("number", sa.String(16), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_phone_numbers_tenant_id_id"),
        sa.CheckConstraint("number ~ '^\\+[1-9][0-9]{7,14}$'", name="e164"),
    )
    op.create_table(
        "calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("phone_number_id", sa.Uuid(), nullable=False),
        sa.Column("provider_call_id", sa.String(34), nullable=False, unique=True),
        sa.Column("stream_sid", sa.String(34), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_frames", sa.Integer(), nullable=False),
        sa.Column("sent_frames", sa.Integer(), nullable=False),
        sa.Column("end_reason", sa.String(40), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "phone_number_id"], ["phone_numbers.tenant_id", "phone_numbers.id"]
        ),
        sa.CheckConstraint(
            "status IN ('received', 'active', 'completed', 'disconnected', 'failed')",
            name="status_allowed",
        ),
        sa.CheckConstraint("received_frames >= 0 AND sent_frames >= 0", name="frame_counts"),
    )
    for table in ("phone_numbers", "calls"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
        # Phone assignment is platform-only; tenant scope can read phone bindings.
        if table == "calls":
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING ({predicate}) WITH CHECK ({predicate})"
            )
        else:
            op.execute(f"CREATE POLICY tenant_phone_read ON {table} FOR SELECT USING ({predicate})")
    platform = """EXISTS (SELECT 1 FROM public.platform_users p
        WHERE p.id = nullif(current_setting('app.platform_user_id', true), '')::uuid
        AND p.is_active AND p.role = 'super_admin')"""
    op.execute(
        "CREATE POLICY platform_phone_management ON phone_numbers "
        f"USING ({platform}) WITH CHECK ({platform})"
    )
    op.execute("""CREATE POLICY inbound_phone_lookup ON phone_numbers FOR SELECT
        USING (number = nullif(current_setting('app.inbound_number', true), ''))""")


def downgrade() -> None:
    op.drop_table("calls")
    op.drop_table("phone_numbers")
