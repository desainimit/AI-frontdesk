"""Provider accounts and product settings the platform administrators manage in the product.

Replaces the provider environment variables. `platform_settings` always holds one row,
created here with the product's starting values.

Revision ID: 0004_platform_settings
Revises: 0003_clinic_settings
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_platform_settings"
down_revision: str | None = "0003_clinic_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# AI models a clinic's own connection may choose from, until an administrator edits them.
DEFAULT_MODELS = (
    '["gemini-3.8-flash", "gemini-3.5-flash-lite", '
    '"google/gemini-3.8-flash", "google/gemini-3.5-flash-lite"]'
)


def upgrade() -> None:
    op.create_table(
        "platform_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("account_identifier", sa.String(64)),
        sa.Column(
            "config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("display_suffix", sa.String(8), server_default="", nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(40)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by", sa.Uuid()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_connections")),
        sa.UniqueConstraint("purpose", name=op.f("uq_platform_connections_purpose")),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["accounts.id"],
            name=op.f("fk_platform_connections_updated_by_accounts"),
        ),
        sa.CheckConstraint(
            "purpose IN ('telephony', 'stt', 'tts', 'llm', 'email')",
            name=op.f("ck_platform_connections_purpose_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('saved_unverified', 'verified', 'invalid')",
            name=op.f("ck_platform_connections_status_allowed"),
        ),
    )
    op.create_table(
        "platform_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("product_name", sa.String(100), nullable=False),
        sa.Column("support_email", sa.String(254), server_default="", nullable=False),
        sa.Column("allowed_llm_models", postgresql.JSONB(), nullable=False),
        sa.Column("session_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_settings")),
        sa.CheckConstraint("id = 1", name=op.f("ck_platform_settings_single_row")),
        sa.CheckConstraint(
            "session_minutes BETWEEN 5 AND 720",
            name=op.f("ck_platform_settings_session_minutes_range"),
        ),
    )
    op.execute(
        "INSERT INTO platform_settings (id, product_name, allowed_llm_models) "
        f"VALUES (1, 'AI Frontdesk', '{DEFAULT_MODELS}'::jsonb)"
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
    op.drop_table("platform_connections")
