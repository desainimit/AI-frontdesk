"""What each reply cost, models chosen from the catalog, and the model behind each vector.

Revision ID: 0014_model_usage
Revises: 0013_speaking_speed
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_model_usage"
down_revision: str | None = "0013_speaking_speed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_turns", sa.Column("cached_tokens", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "ai_turns",
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
    )
    op.add_column(
        "ai_turns",
        sa.Column("checks_cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
    )
    op.add_column(
        "ai_turns", sa.Column("served_by", sa.String(length=60), server_default="", nullable=False)
    )
    # The models a clinic may choose now come from the catalog in the code.
    op.drop_column("platform_settings", "allowed_llm_models")
    # Which model made each vector, so a change of model re-indexes instead of mixing.
    op.add_column(
        "document_chunks",
        sa.Column("embedding_model", sa.String(length=100), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "allowed_llm_models",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text(
                '\'["gemini-3.8-flash", "gemini-3.5-flash-lite", '
                '"google/gemini-3.8-flash", "google/gemini-3.5-flash-lite"]\'::jsonb'
            ),
            nullable=False,
        ),
    )
    op.drop_column("document_chunks", "embedding_model")
    op.drop_column("ai_turns", "served_by")
    op.drop_column("ai_turns", "checks_cost_usd")
    op.drop_column("ai_turns", "cost_usd")
    op.drop_column("ai_turns", "cached_tokens")
