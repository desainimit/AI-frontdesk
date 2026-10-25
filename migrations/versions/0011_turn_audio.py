"""When the caller heard each reply begin, and whether they spoke over it.

Revision ID: 0011_turn_audio
Revises: 0010_integration_platform_reset
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_turn_audio"
down_revision: str | None = "0010_integration_platform_reset"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_turns", sa.Column("first_audio_ms", sa.Integer(), nullable=True))
    op.add_column(
        "ai_turns",
        sa.Column("interrupted", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("ai_turns", "interrupted")
    op.drop_column("ai_turns", "first_audio_ms")
