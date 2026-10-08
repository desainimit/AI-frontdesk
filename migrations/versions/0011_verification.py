"""Configurable call verification and optional patient verification attributes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_verification"
down_revision = "0010_safety"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "verification_policy", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )
    op.add_column("patients", sa.Column("date_of_birth", sa.Date()))
    op.add_column("patients", sa.Column("account_identifier", sa.String(64)))
    op.create_unique_constraint(
        "uq_patients_account_identifier", "patients", ["tenant_id", "account_identifier"]
    )
    op.add_column("calls", sa.Column("verification_expires_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("calls", "verification_expires_at")
    op.drop_constraint("uq_patients_account_identifier", "patients", type_="unique")
    op.drop_column("patients", "account_identifier")
    op.drop_column("patients", "date_of_birth")
    op.drop_column("tenants", "verification_policy")
