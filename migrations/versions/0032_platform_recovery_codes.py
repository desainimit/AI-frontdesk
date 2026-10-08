"""Hash-only, bounded single-use recovery codes for platform authenticators."""

from alembic import op

revision = "0032_platform_recovery"
down_revision = "0031_configuration_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_authenticators "
        "ADD COLUMN recovery_digests jsonb NOT NULL DEFAULT '[]'::jsonb, "
        "ADD COLUMN recovery_revision integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE platform_authenticators "
        "ADD CONSTRAINT ck_platform_authenticators_recovery_revision_nonnegative "
        "CHECK (recovery_revision >= 0), "
        "ADD CONSTRAINT ck_platform_authenticators_recovery_codes_bounded "
        "CHECK (jsonb_typeof(recovery_digests) = 'array' "
        "AND jsonb_array_length(recovery_digests) <= 8)"
    )


def downgrade() -> None:
    raise RuntimeError("Recovery state cannot be downgraded safely. Restore a rehearsed backup.")
