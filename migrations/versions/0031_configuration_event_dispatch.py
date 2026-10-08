"""Bound each durable event to one publication and index the pending queue."""

from alembic import op

revision = "0031_configuration_events"
down_revision = "0030_section_permissions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE configuration_events ADD CONSTRAINT uq_configuration_events_publication "
        "UNIQUE (tenant_id,configuration_id)"
    )
    op.execute(
        "CREATE INDEX ix_configuration_events_pending ON configuration_events "
        "(available_at,created_at,version) WHERE delivered_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_configuration_events_pending")
    op.execute(
        "ALTER TABLE configuration_events DROP CONSTRAINT uq_configuration_events_publication"
    )
