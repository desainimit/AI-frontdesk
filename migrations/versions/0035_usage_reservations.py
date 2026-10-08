"""Tenant-isolated usage reservations and immutable command receipts."""

from alembic import op

revision = "0035_usage_reservations"
down_revision = "0034_billing_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE usage_reservations (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id), resource_key uuid NOT NULL,
        revision integer NOT NULL, meter varchar(30) NOT NULL, state varchar(20) NOT NULL,
        plan_version_id uuid NOT NULL REFERENCES billing_plan_versions(id),
        period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
        configuration_version integer NOT NULL, max_units bigint NOT NULL,
        price_minor bigint NOT NULL, quoted_minor bigint NOT NULL, actual_units bigint,
        lease_expires_at timestamptz NOT NULL, fence uuid, settled_at timestamptz,
        outcome_reference varchar(200), created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_usage_reservations_resource UNIQUE (tenant_id, resource_key),
        CONSTRAINT uq_usage_reservations_tenant_id_id UNIQUE (tenant_id, id),
        CONSTRAINT ck_usage_reservations_meter CHECK (meter IN
            ('voice_seconds', 'sms_messages', 'email_messages', 'chat_messages')),
        CONSTRAINT ck_usage_reservations_state CHECK (state IN
            ('held', 'running', 'reconciliation', 'settled', 'released')),
        CONSTRAINT ck_usage_reservations_quote CHECK (revision >= 1 AND
            max_units BETWEEN 1 AND 100000000 AND price_minor BETWEEN 0 AND 1000000
            AND quoted_minor = max_units * price_minor AND quoted_minor <= 1000000000000
            AND period_end > period_start AND configuration_version >= 1),
        CONSTRAINT ck_usage_reservations_outcome CHECK (
            (state IN ('held', 'running', 'reconciliation') AND actual_units IS NULL
                AND settled_at IS NULL AND outcome_reference IS NULL) OR
            (state = 'settled' AND actual_units BETWEEN 0 AND max_units
                AND settled_at IS NOT NULL AND outcome_reference IS NOT NULL) OR
            (state = 'released' AND actual_units = 0 AND settled_at IS NOT NULL
                AND outcome_reference IS NOT NULL)),
        CONSTRAINT ck_usage_reservations_fence CHECK ((state = 'held' AND fence IS NULL)
            OR (state IN ('running', 'reconciliation', 'settled') AND fence IS NOT NULL)
            OR state = 'released'))""")
    op.execute("""CREATE TABLE usage_commands (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id uuid NOT NULL REFERENCES tenants(id), mutation_id uuid NOT NULL,
        command_hash varchar(64) NOT NULL, request_id uuid NOT NULL, result jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT uq_usage_commands_mutation UNIQUE (tenant_id, mutation_id))""")
    op.execute("""CREATE INDEX ix_usage_reservations_period ON usage_reservations
        (tenant_id, period_start, meter, state)""")
    scope = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for table in ("usage_reservations", "usage_commands"):
        op.execute(f"CREATE INDEX ix_{table}_tenant_id ON {table} (tenant_id)")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ({scope}) WITH CHECK ({scope})"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_mutation()"
        )
    op.execute("""CREATE TRIGGER usage_commands_immutable BEFORE UPDATE OR DELETE ON usage_commands
        FOR EACH ROW EXECUTE FUNCTION public.reject_audit_mutation()""")
    op.execute("""CREATE FUNCTION public.validate_usage_transition() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Usage history cannot be deleted'; END IF;
            IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id
                OR NEW.resource_key <> OLD.resource_key OR NEW.meter <> OLD.meter
                OR NEW.plan_version_id <> OLD.plan_version_id
                OR NEW.period_start <> OLD.period_start OR NEW.period_end <> OLD.period_end
                OR NEW.configuration_version <> OLD.configuration_version
                OR NEW.max_units <> OLD.max_units OR NEW.price_minor <> OLD.price_minor
                OR NEW.quoted_minor <> OLD.quoted_minor OR NEW.created_at <> OLD.created_at
                OR NEW.revision <> OLD.revision + 1 OR OLD.state IN ('settled', 'released')
                OR (OLD.fence IS NOT NULL AND NEW.fence IS DISTINCT FROM OLD.fence)
                OR NOT ((OLD.state = 'held' AND NEW.state IN ('running', 'released'))
                    OR (OLD.state = 'running' AND NEW.state IN
                        ('running', 'reconciliation', 'settled'))
                    OR (OLD.state = 'reconciliation' AND NEW.state = 'settled')) THEN
                RAISE EXCEPTION 'Invalid usage transition';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER usage_reservations_transition BEFORE UPDATE OR DELETE
        ON usage_reservations FOR EACH ROW EXECUTE FUNCTION public.validate_usage_transition()""")
    op.execute("""CREATE FUNCTION public.validate_usage_projection() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE wallet bigint; holds bigint;
        BEGIN
            SELECT reserved_minor INTO wallet FROM public.billing_accounts
                WHERE tenant_id = NEW.tenant_id;
            SELECT coalesce(sum(quoted_minor), 0) INTO holds FROM public.usage_reservations
                WHERE tenant_id = NEW.tenant_id AND state IN ('held', 'running', 'reconciliation');
            IF coalesce(wallet, 0) <> holds THEN
                RAISE EXCEPTION 'Reserved balance does not match usage reservations';
            END IF;
            RETURN NULL;
        END $$""")
    for table in ("usage_reservations", "billing_accounts"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {table}_usage_projection AFTER INSERT OR UPDATE "
            f"ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION public.validate_usage_projection()"
        )
    op.execute("ALTER TABLE billing_ledger DROP CONSTRAINT ck_billing_ledger_entry_shape")
    op.execute("""ALTER TABLE billing_ledger ADD CONSTRAINT ck_billing_ledger_entry_shape CHECK (
        (kind = 'adjustment' AND amount_minor <> 0 AND reserved_delta_minor = 0
            AND platform_user_id IS NOT NULL) OR
        (kind IN ('subscription', 'usage') AND amount_minor <= 0 AND reserved_delta_minor <= 0) OR
        (kind = 'hold' AND amount_minor = 0 AND reserved_delta_minor >= 0) OR
        (kind = 'release' AND amount_minor = 0 AND reserved_delta_minor <= 0))""")


def downgrade() -> None:
    raise RuntimeError("Usage and financial history require a verified backup, not a downgrade.")
