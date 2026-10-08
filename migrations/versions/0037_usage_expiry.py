"""Index bounded global expiry discovery; discovery grants are provisioned per worker role."""

from alembic import op

revision = "0037_usage_expiry"
down_revision = "0036_usage_accounting_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE INDEX ix_usage_reservations_expiry ON usage_reservations
        (lease_expires_at, tenant_id) WHERE state IN ('held', 'running')""")


def downgrade() -> None:
    op.drop_index("ix_usage_reservations_expiry", table_name="usage_reservations")
