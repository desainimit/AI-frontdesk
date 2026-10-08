"""Tenant-owned agent/channel resources and pinned call routing scopes."""

from alembic import op

revision = "0026_configuration_scopes"
down_revision = "0025_erase_reset_mail"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE configuration_resources (
        id uuid CONSTRAINT pk_configuration_resources PRIMARY KEY,
        tenant_id uuid NOT NULL REFERENCES tenants(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        kind varchar(20) NOT NULL, channel_kind varchar(20), name varchar(200) NOT NULL,
        revision integer NOT NULL, is_active boolean NOT NULL, creation_hash varchar(64) NOT NULL,
        CONSTRAINT uq_configuration_resources_tenant_id_id UNIQUE(tenant_id,id),
        CONSTRAINT uq_configuration_resources_name UNIQUE(tenant_id,kind,name),
        CONSTRAINT ck_configuration_resources_resource_kind CHECK(kind IN ('agent','channel')),
        CONSTRAINT ck_configuration_resources_channel_kind CHECK(
            (kind = 'agent' AND channel_kind IS NULL) OR
            (kind = 'channel' AND channel_kind IS NOT NULL AND channel_kind IN ('voice','chat'))),
        CONSTRAINT ck_configuration_resources_resource_revision CHECK(revision >= 1)
    )""")
    op.execute(
        "CREATE INDEX ix_configuration_resources_tenant_id ON configuration_resources(tenant_id)"
    )
    op.execute("ALTER TABLE configuration_resources ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE configuration_resources FORCE ROW LEVEL SECURITY")
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        f"CREATE POLICY tenant_isolation ON configuration_resources USING ({predicate}) "
        f"WITH CHECK ({predicate})"
    )
    for column, target in (
        ("location_id", "locations"),
        ("agent_id", "configuration_resources"),
        ("channel_id", "configuration_resources"),
    ):
        op.execute(f"ALTER TABLE calls ADD COLUMN {column} uuid")
        op.execute(
            f"ALTER TABLE calls ADD CONSTRAINT fk_calls_scope_{column} "
            f"FOREIGN KEY(tenant_id,{column}) REFERENCES {target}(tenant_id,id)"
        )


def downgrade() -> None:
    for column in ("channel_id", "agent_id", "location_id"):
        op.execute(f"ALTER TABLE calls DROP CONSTRAINT fk_calls_scope_{column}")
        op.execute(f"ALTER TABLE calls DROP COLUMN {column}")
    op.execute("DROP TABLE configuration_resources")
