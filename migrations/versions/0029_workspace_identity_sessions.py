"""Password-proven identity admission and an immutable accepted-membership index."""

from alembic import op

revision = "0029_workspace_identity"
down_revision = "0028_business_directory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE identity_memberships (
        id uuid CONSTRAINT pk_identity_memberships PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        identity_id uuid NOT NULL REFERENCES human_identities(id),
        tenant_id uuid NOT NULL, user_id uuid NOT NULL,
        CONSTRAINT uq_identity_memberships_member UNIQUE (tenant_id,user_id),
        CONSTRAINT fk_identity_memberships_tenant_id_human_accounts
        FOREIGN KEY (tenant_id,user_id,identity_id)
        REFERENCES human_accounts(tenant_id,user_id,identity_id)
    )""")
    op.execute(
        "CREATE INDEX ix_identity_memberships_identity_id ON identity_memberships(identity_id)"
    )
    op.execute("""INSERT INTO identity_memberships(identity_id,tenant_id,user_id)
        SELECT identity_id,tenant_id,user_id FROM human_accounts""")
    op.execute("""CREATE TABLE identity_sessions (
        id uuid CONSTRAINT pk_identity_sessions PRIMARY KEY DEFAULT gen_random_uuid(),
        created_at timestamptz NOT NULL DEFAULT now(),
        identity_id uuid NOT NULL REFERENCES human_identities(id),
        credential_revision integer NOT NULL, token_digest varchar(64) NOT NULL,
        request_id uuid NOT NULL, expires_at timestamptz NOT NULL, revoked_at timestamptz
    )""")
    op.execute("CREATE INDEX ix_identity_sessions_identity_id ON identity_sessions(identity_id)")
    identity = """EXISTS(SELECT 1 FROM human_identities i WHERE i.id = identity_id
        AND i.email = nullif(current_setting('app.identity_email',true),''))"""
    for name in ("identity_memberships", "identity_sessions"):
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY identity_scope ON {name} USING ({identity}) WITH CHECK ({identity})"
        )
    # Invoker privileges and both identity and tenant RLS apply to acceptance.
    op.execute("""CREATE FUNCTION index_accepted_membership() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path = public, pg_temp AS $$
        BEGIN
            INSERT INTO public.identity_memberships(identity_id,tenant_id,user_id)
            VALUES(NEW.identity_id,NEW.tenant_id,NEW.user_id);
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER accepted_identity_membership AFTER INSERT ON human_accounts
        FOR EACH ROW EXECUTE FUNCTION index_accepted_membership()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER accepted_identity_membership ON human_accounts")
    op.execute("DROP FUNCTION index_accepted_membership()")
    op.drop_table("identity_sessions")
    op.drop_table("identity_memberships")
