"""What the worker last saw of its queues, and the receptionist's numbers for administrators.

Revision ID: 0009_system_health
Revises: 0008_assistant
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_system_health"
down_revision: str | None = "0008_assistant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A platform administrator, named by the transaction-local setting the API sets.
PLATFORM_ADMIN = (
    "EXISTS (SELECT 1 FROM accounts a WHERE a.id = "
    "NULLIF(current_setting('app.platform_user_id', true), '')::uuid "
    "AND a.is_active AND a.is_platform_admin)"
)


def upgrade() -> None:
    op.create_table(
        "platform_status",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("worker_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queues", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_status")),
        sa.CheckConstraint("id = 1", name=op.f("ck_platform_status_single_row")),
    )
    # Reply counts and timings hold no words; administrators read them across clinics
    # to see whether the receptionist is working.
    op.execute(f"CREATE POLICY platform_health ON ai_turns FOR SELECT USING ({PLATFORM_ADMIN})")


def downgrade() -> None:
    op.execute("DROP POLICY platform_health ON ai_turns")
    op.drop_table("platform_status")
