"""Tenant-controlled human transfer destination. No transfer occurs without one."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0010_safety"
down_revision = "0009_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("human_transfer_number", sa.String(16)))
    op.create_check_constraint(
        "human_transfer_e164",
        "tenants",
        "human_transfer_number IS NULL OR human_transfer_number ~ '^\\+[1-9][0-9]{7,14}$'",
    )


def downgrade() -> None:
    op.drop_constraint("human_transfer_e164", "tenants", type_="check")
    op.drop_column("tenants", "human_transfer_number")
