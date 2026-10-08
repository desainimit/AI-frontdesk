"""Align the persisted MFA uniqueness constraint with model metadata."""

from alembic import op

revision = "0022_mfa_constraint_name"
down_revision = "0021_platform_mfa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE platform_authenticators RENAME CONSTRAINT "
        "platform_authenticators_user_id_key TO uq_platform_authenticators_user_id"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE platform_authenticators RENAME CONSTRAINT "
        "uq_platform_authenticators_user_id TO platform_authenticators_user_id_key"
    )
