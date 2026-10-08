"""Immutable platform pricing and tenant-isolated financial accounting."""

from alembic import op

revision = "0034_billing_accounts"
down_revision = "0033_tenant_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE billing_plan_versions (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(), key varchar(64) NOT NULL,
version integer NOT NULL CHECK (version >= 1), definition jsonb NOT NULL,
mutation_id uuid NOT NULL UNIQUE, command_hash varchar(64) NOT NULL,
platform_user_id uuid NOT NULL REFERENCES platform_users(id), request_id uuid NOT
NULL,
created_at timestamptz NOT NULL DEFAULT now(),
CONSTRAINT uq_billing_plan_versions_key_version UNIQUE (key, version))"""
    )
    op.execute(
        """CREATE TABLE billing_accounts (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES
tenants(id),
revision integer NOT NULL, currency varchar(3) NOT NULL,
balance_minor bigint NOT NULL, reserved_minor bigint NOT NULL,
plan_version_id uuid REFERENCES billing_plan_versions(id),
period_start timestamptz, period_end timestamptz,
created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT
now(),
CONSTRAINT uq_billing_accounts_tenant_id UNIQUE (tenant_id),
CONSTRAINT ck_billing_accounts_valid_balance CHECK (revision >= 0 AND balance_minor
BETWEEN 0 AND 1000000000000 AND reserved_minor BETWEEN 0 AND balance_minor),
CONSTRAINT ck_billing_accounts_currency CHECK (currency = 'INR'),
CONSTRAINT ck_billing_accounts_subscription_period CHECK ((plan_version_id IS NULL AND
period_start IS NULL AND period_end IS NULL) OR (plan_version_id IS NOT NULL AND
period_start IS NOT NULL AND period_end > period_start)))"""
    )
    op.execute(
        """CREATE TABLE billing_ledger (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES
tenants(id),
revision integer NOT NULL, kind varchar(20) NOT NULL, mutation_id uuid NOT NULL,
amount_minor bigint NOT NULL, reserved_delta_minor bigint NOT NULL,
balance_after_minor bigint NOT NULL, reserved_after_minor bigint NOT NULL,
reason varchar(500) NOT NULL, actor_id uuid,
platform_user_id uuid REFERENCES platform_users(id), request_id uuid NOT NULL,
created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT
now(),
CONSTRAINT uq_billing_ledger_tenant_revision UNIQUE (tenant_id, revision),
CONSTRAINT uq_billing_ledger_mutation UNIQUE (tenant_id, mutation_id),
CONSTRAINT ck_billing_ledger_valid_balance CHECK (revision >= 1 AND
balance_after_minor BETWEEN 0 AND 1000000000000 AND reserved_after_minor BETWEEN 0 AND
balance_after_minor),
CONSTRAINT ck_billing_ledger_kind_allowed CHECK (kind IN ('subscription',
'adjustment', 'usage', 'hold', 'release')),
CONSTRAINT ck_billing_ledger_entry_shape CHECK ((kind = 'adjustment' AND amount_minor
<> 0 AND reserved_delta_minor = 0 AND platform_user_id IS NOT NULL) OR (kind IN
('subscription', 'usage') AND amount_minor <= 0 AND reserved_delta_minor <= 0) OR
(kind = 'hold' AND amount_minor = 0 AND reserved_delta_minor > 0) OR (kind = 'release'
AND amount_minor = 0 AND reserved_delta_minor < 0)))"""
    )
    op.execute(
        """CREATE TABLE billing_mutations (
id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES
tenants(id),
mutation_id uuid NOT NULL, command_hash varchar(64) NOT NULL,
actor_key varchar(100) NOT NULL, result jsonb NOT NULL,
created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT
now(),
CONSTRAINT uq_billing_mutations_tenant_mutation UNIQUE (tenant_id, mutation_id))"""
    )
    platform = (
        "EXISTS (SELECT 1 FROM public.platform_users p JOIN public.platfo"
        "rm_sessions s ON s.user_id = p.id WHERE\n        p.id = nullif(cu"
        "rrent_setting('app.platform_user_id', true), '')::uuid\n        A"
        "ND p.is_active AND p.role = 'super_admin'\n        AND s.id = nul"
        "lif(current_setting('app.financial_platform_session_id', true), "
        "'')::uuid\n        AND s.revoked_at IS NULL AND s.expires_at > no"
        "w() AND s.mfa_verified_at IS NOT NULL)"
    )
    op.execute("""ALTER TABLE billing_plan_versions ENABLE ROW LEVEL SECURITY""")
    op.execute("""ALTER TABLE billing_plan_versions FORCE ROW LEVEL SECURITY""")
    op.execute("""CREATE POLICY pricing_read ON billing_plan_versions FOR SELECT USING (true)""")
    op.execute(
        f"CREATE POLICY pricing_publish ON billing_plan_versions FOR INSERT WITH CHECK ({platform})"
    )
    tenant = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for table in ("billing_accounts", "billing_ledger", "billing_mutations"):
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON {table} (tenant_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ({tenant}) WITH CHECK ({tenant})"
        )
    # The restrictive policy adds platform authority to positive balance changes.
    op.execute(
        "CREATE POLICY adjustment_authority ON billing_ledger AS RESTRICTIVE FOR INSERT "
        f"WITH CHECK (kind <> 'adjustment' OR ({platform}))"
    )
    for table in ("billing_plan_versions", "billing_ledger", "billing_mutations"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION public.reject_audit_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_mutation()"
        )
    op.execute(
        """CREATE FUNCTION public.validate_billing_entry() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE prior public.billing_ledger%ROWTYPE;
BEGIN
SELECT * INTO prior FROM public.billing_ledger WHERE tenant_id = NEW.tenant_id ORDER
BY revision DESC LIMIT 1;
IF NEW.revision <> coalesce(prior.revision, 0) + 1
OR NEW.balance_after_minor <> coalesce(prior.balance_after_minor, 0) +
NEW.amount_minor
OR NEW.reserved_after_minor <> coalesce(prior.reserved_after_minor, 0) +
NEW.reserved_delta_minor THEN
RAISE EXCEPTION 'Invalid financial ledger chain';
END IF;
RETURN NEW;
END $$"""
    )
    op.execute(
        """CREATE TRIGGER billing_ledger_chain BEFORE INSERT ON billing_ledger FOR EACH ROW
EXECUTE FUNCTION public.validate_billing_entry()"""
    )
    op.execute(
        """CREATE FUNCTION public.validate_billing_projection() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
DECLARE account public.billing_accounts%ROWTYPE; latest public.billing_ledger%ROWTYPE;
BEGIN
SELECT * INTO account FROM public.billing_accounts WHERE tenant_id = NEW.tenant_id;
SELECT * INTO latest FROM public.billing_ledger WHERE tenant_id = NEW.tenant_id ORDER
BY revision DESC LIMIT 1;
IF account.id IS NULL OR account.revision <> coalesce(latest.revision, 0)
OR account.balance_minor <> coalesce(latest.balance_after_minor, 0)
OR account.reserved_minor <> coalesce(latest.reserved_after_minor, 0) THEN
RAISE EXCEPTION 'Financial projection does not match immutable ledger';
END IF;
RETURN NULL;
END $$"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER billing_account_projection AFTER INSERT OR UPDATE ON
billing_accounts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
public.validate_billing_projection()"""
    )
    op.execute(
        """CREATE CONSTRAINT TRIGGER billing_ledger_projection AFTER INSERT ON billing_ledger
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
public.validate_billing_projection()"""
    )


def downgrade() -> None:
    raise RuntimeError(
        "Financial history is immutable; restore a verified backup instead of downgrading."
    )
