"""One-step clinic creation and editable defaults for new clinics.

The onboarding draft is gone: the creation record keeps only who created the clinic,
with which request, and its first owner and location. `platform_settings` gains the
administrators' own defaults for new clinics (empty while the product's are in use).

Revision ID: 0005_workspace_creation
Revises: 0004_platform_settings
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_workspace_creation"
down_revision: str | None = "0004_platform_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_tenant_onboardings_onboarding_status"), "tenant_onboardings", type_="check"
    )
    op.drop_constraint(
        op.f("ck_tenant_onboardings_positive_revision"), "tenant_onboardings", type_="check"
    )
    for column in ("draft", "revision", "status"):
        op.drop_column("tenant_onboardings", column)
    op.add_column("platform_settings", sa.Column("workspace_defaults", postgresql.JSONB()))


def downgrade() -> None:
    op.drop_column("platform_settings", "workspace_defaults")
    op.add_column(
        "tenant_onboardings",
        sa.Column("status", sa.String(30), server_default="draft", nullable=False),
    )
    op.add_column(
        "tenant_onboardings",
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "tenant_onboardings",
        sa.Column("draft", postgresql.JSONB(), server_default="{}", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_tenant_onboardings_positive_revision"), "tenant_onboardings", "revision >= 1"
    )
    op.create_check_constraint(
        op.f("ck_tenant_onboardings_onboarding_status"),
        "tenant_onboardings",
        "status IN ('draft', 'invited')",
    )
