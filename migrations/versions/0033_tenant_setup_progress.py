"""Tenant-isolated, revision-checked setup navigation and evidence review."""

from alembic import op

revision = "0033_tenant_setup"
down_revision = "0032_platform_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE tenant_setup_progress (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        revision integer NOT NULL,
        current_step varchar(32) NOT NULL,
        reviewed jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_tenant_setup_progress_tenant_id UNIQUE (tenant_id),
        CONSTRAINT ck_tenant_setup_progress_positive_revision CHECK (revision >= 1),
        CONSTRAINT ck_tenant_setup_progress_reviewed_object
            CHECK (jsonb_typeof(reviewed) = 'object'),
        CONSTRAINT ck_tenant_setup_progress_step_allowed CHECK
            (current_step IN ('business', 'administrator', 'team', 'agent',
            'knowledge', 'channels', 'billing', 'tests', 'publication')))""")
    op.execute("ALTER TABLE tenant_setup_progress ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_setup_progress FORCE ROW LEVEL SECURITY")
    tenant = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        f"CREATE POLICY tenant_isolation ON tenant_setup_progress "
        f"USING ({tenant}) WITH CHECK ({tenant})"
    )


def downgrade() -> None:
    op.drop_table("tenant_setup_progress")
