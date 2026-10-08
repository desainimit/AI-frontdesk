"""Global password identities with separately isolated tenant memberships."""

from alembic import op

revision = "0024_global_identity"
down_revision = "0023_saas_activation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE human_identities (
        id uuid CONSTRAINT pk_human_identities PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        email varchar(254) NOT NULL CONSTRAINT uq_human_identities_email UNIQUE,
        password_hash varchar(256), credential_revision integer NOT NULL DEFAULT 1,
        CONSTRAINT ck_human_identities_normalized_email CHECK (email = lower(email)),
        CONSTRAINT ck_human_identities_credential_revision_positive CHECK (credential_revision >= 1)
    )""")
    # Conflicting salted hashes require email-verified recovery, not a guessed winner.
    op.execute("""INSERT INTO human_identities (email, password_hash)
        SELECT lower(u.email), CASE WHEN count(DISTINCT h.password_hash) = 1
        THEN min(h.password_hash) ELSE NULL END
        FROM human_accounts h JOIN users u ON u.id = h.user_id AND u.tenant_id = h.tenant_id
        GROUP BY lower(u.email)""")
    op.execute("ALTER TABLE human_accounts ADD COLUMN identity_id uuid")
    op.execute("""UPDATE human_accounts h SET identity_id = i.id FROM users u, human_identities i
        WHERE u.id = h.user_id AND u.tenant_id = h.tenant_id AND i.email = lower(u.email)""")
    op.execute("ALTER TABLE human_accounts ALTER COLUMN identity_id SET NOT NULL")
    op.execute("""ALTER TABLE human_accounts ADD CONSTRAINT
        fk_human_accounts_identity_id_human_identities
        FOREIGN KEY (identity_id) REFERENCES human_identities(id)""")
    op.execute("""ALTER TABLE human_accounts ADD CONSTRAINT uq_human_accounts_identity
        UNIQUE (tenant_id, user_id, identity_id)""")
    op.execute("ALTER TABLE human_sessions ADD COLUMN identity_id uuid")
    op.execute("ALTER TABLE human_sessions ADD COLUMN credential_revision integer")
    op.execute("""UPDATE human_sessions s SET identity_id = h.identity_id, credential_revision = 1
        FROM human_accounts h WHERE h.user_id = s.user_id AND h.tenant_id = s.tenant_id""")
    op.execute("ALTER TABLE human_sessions ALTER COLUMN identity_id SET NOT NULL")
    op.execute("ALTER TABLE human_sessions ALTER COLUMN credential_revision SET NOT NULL")
    op.execute("""ALTER TABLE human_sessions ADD CONSTRAINT
        fk_human_sessions_identity_id_human_identities
        FOREIGN KEY (identity_id) REFERENCES human_identities(id)""")
    op.execute("""ALTER TABLE human_sessions ADD CONSTRAINT
        fk_human_sessions_tenant_id_human_accounts
        FOREIGN KEY (tenant_id, user_id, identity_id)
        REFERENCES human_accounts(tenant_id, user_id, identity_id)""")
    op.execute("ALTER TABLE auth_challenges ADD COLUMN credential_revision integer")
    op.execute("""UPDATE auth_challenges SET consumed_at = now()
        WHERE purpose = 'reset' AND consumed_at IS NULL""")
    op.execute("ALTER TABLE human_accounts DROP COLUMN password_hash")
    op.execute("ALTER TABLE human_identities ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE human_identities FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY identity_scope ON human_identities
        USING (email = nullif(current_setting('app.identity_email', true), ''))
        WITH CHECK (email = nullif(current_setting('app.identity_email', true), ''))""")


def downgrade() -> None:
    raise RuntimeError("Global identity cutover is forward-only; restore a verified backup")
