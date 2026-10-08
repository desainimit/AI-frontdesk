"""Distinguish resumable SaaS onboarding from pre-existing operator bootstrap."""

from alembic import op

revision = "0023_saas_activation"
down_revision = "0022_mfa_constraint_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tenants ADD COLUMN requires_saas_activation boolean NOT NULL DEFAULT true"
    )
    op.execute(
        "UPDATE tenants SET requires_saas_activation = false WHERE NOT EXISTS "
        "(SELECT 1 FROM tenant_onboardings WHERE tenant_onboardings.tenant_id = tenants.id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN requires_saas_activation")
