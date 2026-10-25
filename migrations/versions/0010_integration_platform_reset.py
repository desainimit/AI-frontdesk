"""Record an explicit return to platform provider accounts without changing role grants.

Revision ID: 0010_integration_platform_reset
Revises: 0009_system_health
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_integration_platform_reset"
down_revision: str | None = "0009_system_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUSES = "'not_configured', 'saved_unverified', 'verified', 'invalid', 'revoked'"
CONSTRAINT = "ck_tenant_integrations_status_allowed"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "tenant_integrations", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "tenant_integrations", f"status IN ({STATUSES}, 'using_platform')"
    )


def downgrade() -> None:
    # Old servers do not understand this state; require a deliberate reconnection.
    op.execute("UPDATE tenant_integrations SET status = 'revoked' WHERE status = 'using_platform'")
    op.drop_constraint(op.f(CONSTRAINT), "tenant_integrations", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "tenant_integrations", f"status IN ({STATUSES})")
