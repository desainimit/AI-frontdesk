"""Erase queued reset payloads invalidated during global identity cutover."""

from alembic import op

revision = "0025_erase_reset_mail"
down_revision = "0024_global_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""UPDATE auth_mail SET status = 'cancelled', ciphertext = NULL
        WHERE challenge_id IN (
            SELECT id FROM auth_challenges WHERE purpose = 'reset'
            AND credential_revision IS NULL AND consumed_at IS NOT NULL
        )""")


def downgrade() -> None:
    # Erased bearer material must not be recreated by a rollback.
    pass
