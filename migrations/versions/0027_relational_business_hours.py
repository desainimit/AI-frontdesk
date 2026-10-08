"""Normalize draft business and location hours without mutating published revisions."""

from alembic import op

revision = "0027_business_hours"
down_revision = "0026_configuration_scopes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    common = (
        "id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now(), "
    )
    definitions = {
        "business_hours_policies": """location_id uuid, after_hours varchar(20) NOT NULL,
            CONSTRAINT uq_business_hours_policies_tenant_id_id UNIQUE(tenant_id,id),
            CONSTRAINT uq_business_hours_policies_location
                UNIQUE NULLS NOT DISTINCT(tenant_id,location_id),
            CONSTRAINT fk_business_hours_policies_location FOREIGN KEY(tenant_id,location_id)
                REFERENCES locations(tenant_id,id),
            CONSTRAINT ck_business_hours_policies_after_hours
                CHECK(after_hours IN ('answer_only','callback','transfer'))""",
        "business_opening_windows": """policy_id uuid NOT NULL, weekday integer NOT NULL,
            opens time NOT NULL, closes time NOT NULL,
            CONSTRAINT fk_business_opening_windows_policy FOREIGN KEY(tenant_id,policy_id)
                REFERENCES business_hours_policies(tenant_id,id),
            CONSTRAINT ck_business_opening_windows_weekday CHECK(weekday BETWEEN 0 AND 6),
            CONSTRAINT ck_business_opening_windows_ordered_window CHECK(opens < closes)""",
        "business_holidays": """policy_id uuid NOT NULL, day date NOT NULL,
            CONSTRAINT fk_business_holidays_policy FOREIGN KEY(tenant_id,policy_id)
                REFERENCES business_hours_policies(tenant_id,id),
            CONSTRAINT uq_business_holidays_day UNIQUE(tenant_id,policy_id,day)""",
    }
    for name, definition in definitions.items():
        op.execute(f"CREATE TABLE {name} ({common}{definition})")
        op.execute(f"CREATE INDEX ix_{name}_tenant_id ON {name}(tenant_id)")
    op.execute("""INSERT INTO business_hours_policies(id,tenant_id,after_hours)
        SELECT gen_random_uuid(),tenant_id,coalesce(document->'hours'->>'after_hours','answer_only')
        FROM configuration_drafts""")
    op.execute("""INSERT INTO business_hours_policies(id,tenant_id,location_id,after_hours)
        SELECT gen_random_uuid(),d.tenant_id,l.id,
            coalesce(s.value->'hours'->>'after_hours','answer_only')
        FROM configuration_drafts d
        CROSS JOIN LATERAL jsonb_each(coalesce(d.document->'scopes'->'locations','{}'::jsonb)) s
        JOIN locations l ON l.tenant_id = d.tenant_id AND l.id::text = s.key
        WHERE s.value->'hours' IS NOT NULL AND s.value->'hours' <> 'null'::jsonb""")
    # Normalize saved drafts; immutable publication documents remain untouched.
    hours = (
        "CASE WHEN p.location_id IS NULL THEN d.document->'hours' "
        "ELSE d.document->'scopes'->'locations'->(p.location_id::text)->'hours' END"
    )
    op.execute(f"""INSERT INTO business_opening_windows(id,tenant_id,policy_id,weekday,opens,closes)
        SELECT gen_random_uuid(),p.tenant_id,p.id,(w->>'weekday')::int,
            (w->>'opens')::time,(w->>'closes')::time
        FROM business_hours_policies p JOIN configuration_drafts d USING(tenant_id)
        CROSS JOIN LATERAL jsonb_array_elements(coalesce(({hours})->'windows','[]'::jsonb)) w""")
    op.execute(f"""INSERT INTO business_holidays(id,tenant_id,policy_id,day)
        SELECT gen_random_uuid(),p.tenant_id,p.id,h::date
        FROM business_hours_policies p JOIN configuration_drafts d USING(tenant_id)
        CROSS JOIN LATERAL jsonb_array_elements_text(
            coalesce(({hours})->'holidays','[]'::jsonb)) h""")
    tenant = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for name in definitions:
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {name} USING ({tenant}) WITH CHECK ({tenant})"
        )


def downgrade() -> None:
    for name in ("business_holidays", "business_opening_windows", "business_hours_policies"):
        op.execute(f"DROP TABLE {name}")
