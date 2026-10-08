"""Require a verified second factor for platform operations and tenant entry."""

from alembic import op

revision = "0021_platform_mfa"
down_revision = "0020_configuration_rbac"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE platform_sessions ADD COLUMN mfa_verified_at timestamptz")
    op.execute("""CREATE TABLE platform_authenticators (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL UNIQUE REFERENCES platform_users(id),
        key_id varchar(64) NOT NULL, ciphertext bytea NOT NULL,
        enabled_at timestamptz, last_counter bigint NOT NULL DEFAULT -1,
        failed_attempts integer NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
        locked_until timestamptz, expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    op.execute("DROP TABLE platform_authenticators")
    op.execute("ALTER TABLE platform_sessions DROP COLUMN mfa_verified_at")
