"""Establish the migration baseline; domain tables begin in Phase 2.

Revision ID: 0001_foundation
Revises: None
"""

from collections.abc import Sequence

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Alembic creates and stamps its version table; no domain schema exists yet."""


def downgrade() -> None:
    """The baseline has no domain schema to remove."""
