"""Messages to patients: confirmations, reminders and change notices, with a delivery log.

Revision ID: 0006_outbound_messages
Revises: 0005_workspace_creation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_outbound_messages"
down_revision: str | None = "0005_workspace_creation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
# A platform administrator, named by the transaction-local setting the API sets.
PLATFORM_ADMIN = (
    "EXISTS (SELECT 1 FROM accounts a WHERE a.id = "
    "NULLIF(current_setting('app.platform_user_id', true), '')::uuid "
    "AND a.is_active AND a.is_platform_admin)"
)


def upgrade() -> None:
    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("recipient", sa.String(254), nullable=False),
        sa.Column("sender", sa.String(16)),
        sa.Column("subject", sa.String(200)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(10), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(40)),
        sa.Column("appointment_id", sa.Uuid()),
        sa.Column("dedupe_key", sa.String(120), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbound_messages")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_outbound_messages_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "appointment_id"],
            ["appointments.tenant_id", "appointments.id"],
            name=op.f("fk_outbound_messages_appointment"),
        ),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name=op.f("uq_outbound_messages_dedupe")),
        sa.CheckConstraint(
            "channel IN ('sms', 'email')", name=op.f("ck_outbound_messages_channel_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'cancelled')",
            name=op.f("ck_outbound_messages_status_allowed"),
        ),
    )
    op.create_index(op.f("ix_outbound_messages_tenant_id"), "outbound_messages", ["tenant_id"])
    op.create_index(
        "ix_outbound_messages_due",
        "outbound_messages",
        ["available_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.execute("ALTER TABLE outbound_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbound_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON outbound_messages "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )
    # Platform administrators read every clinic's delivery log; they do not write it.
    op.execute(
        "CREATE POLICY platform_message_log ON outbound_messages FOR SELECT "
        f"USING ({PLATFORM_ADMIN})"
    )


def downgrade() -> None:
    op.drop_table("outbound_messages")
