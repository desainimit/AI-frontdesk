"""Calendar sync: provider calendars, busy time, and the event written for each appointment.

Revision ID: 0007_calendar_sync
Revises: 0006_outbound_messages
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0007_calendar_sync"
down_revision: str | None = "0006_outbound_messages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = (
    "calendar_connections",
    "calendar_oauth_states",
    "calendar_busy_blocks",
    "appointment_calendar_links",
)
OLD_PURPOSES = "purpose IN ('telephony', 'stt', 'tts', 'llm', 'email')"
NEW_PURPOSES = (
    "purpose IN ('telephony', 'stt', 'tts', 'llm', 'email', "
    "'google_calendar', 'microsoft_calendar')"
)


def tenant_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def provider_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "provider_id"],
        ["providers.tenant_id", "providers.id"],
        name=op.f(f"fk_{table}_provider"),
    )


def tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants")
    )


def upgrade() -> None:
    # The platform's calendar apps are provider accounts too; a Google client ID is long.
    op.drop_constraint(
        op.f("ck_platform_connections_purpose_allowed"), "platform_connections", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_platform_connections_purpose_allowed"), "platform_connections", NEW_PURPOSES
    )
    op.alter_column("platform_connections", "account_identifier", type_=sa.String(200))

    op.create_table(
        "calendar_connections",
        *tenant_columns(),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("account_email", sa.String(254), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(40)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_connections")),
        tenant_fk("calendar_connections"),
        provider_fk("calendar_connections"),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_calendar_connections_tenant_id_id")),
        sa.UniqueConstraint(
            "tenant_id", "provider_id", name=op.f("uq_calendar_connections_provider")
        ),
        sa.CheckConstraint(
            "kind IN ('google', 'microsoft')", name=op.f("ck_calendar_connections_kind_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('connected', 'needs_attention')",
            name=op.f("ck_calendar_connections_status_allowed"),
        ),
    )
    op.create_table(
        "calendar_oauth_states",
        *tenant_columns(),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("state_digest", sa.String(64), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_oauth_states")),
        tenant_fk("calendar_oauth_states"),
        provider_fk("calendar_oauth_states"),
        sa.UniqueConstraint("state_digest", name=op.f("uq_calendar_oauth_states_state")),
    )
    op.create_table(
        "calendar_busy_blocks",
        *tenant_columns(),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calendar_busy_blocks")),
        tenant_fk("calendar_busy_blocks"),
        provider_fk("calendar_busy_blocks"),
        sa.CheckConstraint(
            "ends_at > starts_at", name=op.f("ck_calendar_busy_blocks_positive_duration")
        ),
    )
    op.create_index(
        "ix_calendar_busy_blocks_window",
        "calendar_busy_blocks",
        ["tenant_id", "provider_id", "starts_at"],
    )
    op.create_table(
        "appointment_calendar_links",
        *tenant_columns(),
        sa.Column("appointment_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_event_id", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_appointment_calendar_links")),
        tenant_fk("appointment_calendar_links"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "appointment_id"],
            ["appointments.tenant_id", "appointments.id"],
            name=op.f("fk_appointment_calendar_links_appointment"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["calendar_connections.tenant_id", "calendar_connections.id"],
            name=op.f("fk_appointment_calendar_links_connection"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "tenant_id", "appointment_id", name=op.f("uq_appointment_calendar_links")
        ),
        sa.CheckConstraint(
            "status IN ('synced', 'conflict')",
            name=op.f("ck_appointment_calendar_links_status_allowed"),
        ),
    )
    for table in TABLES:
        op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    op.alter_column("platform_connections", "account_identifier", type_=sa.String(64))
    op.drop_constraint(
        op.f("ck_platform_connections_purpose_allowed"), "platform_connections", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_platform_connections_purpose_allowed"), "platform_connections", OLD_PURPOSES
    )
