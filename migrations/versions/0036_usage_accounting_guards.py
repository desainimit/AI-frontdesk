"""Bind usage charges to immutable receipts without changing historical ledger rows."""

from alembic import op

revision = "0036_usage_accounting_guards"
down_revision = "0035_usage_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE INDEX ix_usage_reservations_active ON usage_reservations
        (tenant_id, meter, lease_expires_at)
        WHERE state IN ('held', 'running', 'reconciliation')""")
    op.execute("""CREATE INDEX ix_usage_reservations_spending ON usage_reservations
        (tenant_id, settled_at) WHERE state = 'settled'""")
    op.execute("""CREATE INDEX ix_usage_commands_reservation ON usage_commands
        (tenant_id, (result->'reservation'->>'id'))""")
    op.execute("""CREATE FUNCTION public.check_usage_financial_record(scope uuid, identity uuid)
        RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE item public.usage_reservations%ROWTYPE;
            holds bigint; releases bigint; settlements bigint;
            money bigint; reserved bigint; malformed boolean;
        BEGIN
            SELECT * INTO item FROM public.usage_reservations
                WHERE tenant_id = scope AND id = identity;
            IF item.id IS NULL THEN RAISE EXCEPTION 'Missing usage reservation'; END IF;
            SELECT count(*) FILTER (WHERE l.kind = 'hold'),
                count(*) FILTER (WHERE l.kind = 'release'),
                count(*) FILTER (WHERE l.kind = 'usage'),
                coalesce(sum(l.amount_minor), 0), coalesce(sum(l.reserved_delta_minor), 0),
                coalesce(bool_or(NOT (
                    (c.result->>'operation' = 'reserve' AND l.kind = 'hold'
                        AND l.amount_minor = 0 AND l.reserved_delta_minor = item.quoted_minor) OR
                    (c.result->>'operation' IN ('release', 'expire') AND l.kind = 'release'
                        AND l.amount_minor = 0 AND l.reserved_delta_minor = -item.quoted_minor) OR
                    (c.result->>'operation' = 'settle' AND l.kind = 'usage'
                        AND l.amount_minor = -item.actual_units * item.price_minor
                        AND l.reserved_delta_minor = -item.quoted_minor))), false)
                INTO holds, releases, settlements, money, reserved, malformed
                FROM public.usage_commands c JOIN public.billing_ledger l
                    ON l.tenant_id = c.tenant_id AND l.mutation_id = c.mutation_id
                WHERE c.tenant_id = scope AND c.result->'reservation'->>'id' = identity::text;
            IF malformed OR holds <> 1
                OR releases <> (CASE WHEN item.state = 'released' THEN 1 ELSE 0 END)
                OR settlements <> (CASE WHEN item.state = 'settled' THEN 1 ELSE 0 END)
                OR money <> (CASE WHEN item.state = 'settled'
                    THEN -item.actual_units * item.price_minor ELSE 0 END)
                OR reserved <> (CASE WHEN item.state IN ('held', 'running', 'reconciliation')
                    THEN item.quoted_minor ELSE 0 END) THEN
                RAISE EXCEPTION 'Usage charges do not match their immutable command receipts';
            END IF;
        END $$""")
    op.execute("""CREATE FUNCTION public.validate_usage_financial_record() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE identity uuid;
        BEGIN
            IF TG_TABLE_NAME = 'billing_ledger' THEN
                IF NEW.kind NOT IN ('hold', 'release', 'usage') THEN RETURN NULL; END IF;
                SELECT (result->'reservation'->>'id')::uuid INTO identity
                    FROM public.usage_commands
                    WHERE tenant_id = NEW.tenant_id AND mutation_id = NEW.mutation_id;
            ELSIF TG_TABLE_NAME = 'usage_commands' THEN
                identity := (NEW.result->'reservation'->>'id')::uuid;
            ELSE identity := NEW.id;
            END IF;
            IF identity IS NULL THEN
                RAISE EXCEPTION 'Usage ledger requires a command receipt';
            END IF;
            PERFORM public.check_usage_financial_record(NEW.tenant_id, identity);
            RETURN NULL;
        END $$""")
    for table, operations in (
        ("usage_reservations", "INSERT OR UPDATE"),
        ("usage_commands", "INSERT"),
        ("billing_ledger", "INSERT"),
    ):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {table}_financial_record AFTER {operations} ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION public.validate_usage_financial_record()"
        )


def downgrade() -> None:
    raise RuntimeError("Financial integrity guards require a verified backup, not a downgrade.")
