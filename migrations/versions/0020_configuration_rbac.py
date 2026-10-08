"""Persisted tenant roles and immutable operational configuration revisions."""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_configuration_rbac"
down_revision: str | None = "0019_tenant_integrations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen migration data: do not import mutable application permission catalogs.
KEYS = (
    "call:read",
    "follow_up:manage",
    "tenant:read",
    "tenant:update",
    "knowledge:read",
    "knowledge:write",
    "user:read",
    "user:invite",
    "user:manage",
    "integration:read",
    "integration:manage",
    "tenant:activate",
    "audit:read",
    "directory:read",
    "directory:write",
    "patient:read",
    "patient:write",
    "appointment:read",
    "appointment:write",
    "role:read",
    "role:create",
    "role:update",
    "role:delete",
    "role:assign",
    "configuration:read",
    "configuration:update",
    "configuration:test",
    "configuration:publish",
    "configuration:rollback",
    "business:read",
    "business:write",
    "conversation:read",
    "conversation:write",
    "ticket:read",
    "ticket:write",
    "workflow:read",
    "workflow:write",
    "billing:read",
    "billing:manage",
    "privacy:export",
    "privacy:delete",
)


def upgrade() -> None:
    common = (
        "id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), "
        "created_at timestamptz NOT NULL DEFAULT now(), "
        "updated_at timestamptz NOT NULL DEFAULT now(), "
    )
    tables = {
        "tenant_roles": "name varchar(100) NOT NULL, description varchar(500) NOT NULL DEFAULT '', "
        "template_key varchar(30), revision integer NOT NULL DEFAULT 1 CHECK(revision > 0), "
        "CONSTRAINT uq_tenant_roles_tenant_id_id UNIQUE(tenant_id,id), "
        "CONSTRAINT uq_tenant_roles_name UNIQUE(tenant_id,name), "
        "CONSTRAINT uq_tenant_roles_template UNIQUE(tenant_id,template_key)",
        "role_permissions": "role_id uuid NOT NULL, key varchar(80) NOT NULL, "
        "allowed boolean NOT NULL DEFAULT false, "
        "CONSTRAINT uq_role_permissions_key UNIQUE(tenant_id,role_id,key), "
        "FOREIGN KEY(tenant_id,role_id) REFERENCES tenant_roles(tenant_id,id)",
        "configuration_drafts": "revision integer NOT NULL CHECK(revision > 0), "
        "document jsonb NOT NULL, defaults_version integer NOT NULL, published_version integer, "
        "CONSTRAINT uq_configuration_drafts_tenant UNIQUE(tenant_id)",
        "published_configurations": "version integer NOT NULL CHECK(version > 0), "
        "source_revision integer NOT NULL, document jsonb NOT NULL, "
        "content_hash varchar(64) NOT NULL, published_by uuid, platform_user_id uuid, "
        "CONSTRAINT uq_published_configurations_version UNIQUE(tenant_id,version), "
        "CONSTRAINT uq_published_configurations_tenant_id_id UNIQUE(tenant_id,id)",
        "configuration_events": "configuration_id uuid NOT NULL, version integer NOT NULL, "
        "delivered_at timestamptz, available_at timestamptz NOT NULL DEFAULT now(), "
        "FOREIGN KEY(tenant_id,configuration_id) "
        "REFERENCES published_configurations(tenant_id,id)",
    }
    for name, definition in tables.items():
        op.execute(f"CREATE TABLE {name} ({common}{definition})")
        op.execute(f"CREATE INDEX ix_{name}_tenant_id ON {name}(tenant_id)")
    # Backfill development memberships under the migration owner before enabling RLS.
    op.execute(
        "INSERT INTO tenant_roles(id,tenant_id,name,template_key) "
        "SELECT gen_random_uuid(), t.id, initcap(v.key), v.key FROM tenants t "
        "CROSS JOIN (VALUES ('owner'),('admin'),('operator'),('analyst'),('billing'),('member')) "
        "AS v(key)"
    )
    values = ",".join(f"('{key}')" for key in KEYS)
    op.execute(
        "INSERT INTO role_permissions(id,tenant_id,role_id,key,allowed) "
        "SELECT gen_random_uuid(), r.tenant_id,r.id,p.key, "
        "r.template_key IN ('owner','admin') OR "
        "(r.template_key = 'member' AND p.key IN "
        "('tenant:read','directory:read','knowledge:read')) OR "
        "(r.template_key = 'operator' AND p.key IN "
        "('tenant:read','directory:read','knowledge:read','call:read','patient:read',"
        "'patient:write','appointment:read','appointment:write','business:read',"
        "'conversation:read','conversation:write','ticket:read','ticket:write')) OR "
        "(r.template_key = 'analyst' AND p.key IN ('tenant:read','audit:read')) OR "
        "(r.template_key = 'billing' AND p.key IN "
        "('tenant:read','billing:read','billing:manage')) "
        f"FROM tenant_roles r CROSS JOIN (VALUES {values}) AS p(key)"
    )
    op.execute("ALTER TABLE users ADD COLUMN role_id uuid")
    op.execute("ALTER TABLE calls ADD COLUMN configuration_version integer")
    op.execute(
        "ALTER TABLE calls ADD CONSTRAINT fk_calls_configuration_version "
        "FOREIGN KEY(tenant_id,configuration_version) "
        "REFERENCES published_configurations(tenant_id,version)"
    )
    op.execute(
        "UPDATE users u SET role_id = r.id FROM tenant_roles r "
        "WHERE r.tenant_id=u.tenant_id AND r.template_key=u.role"
    )
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT fk_users_tenant_role "
        "FOREIGN KEY(tenant_id,role_id) REFERENCES tenant_roles(tenant_id,id)"
    )
    tenant = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for name in tables:
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {name} USING ({tenant}) WITH CHECK ({tenant})"
        )


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP CONSTRAINT fk_calls_configuration_version")
    op.execute("ALTER TABLE calls DROP COLUMN configuration_version")
    op.execute("ALTER TABLE users DROP CONSTRAINT fk_users_tenant_role")
    op.execute("ALTER TABLE users DROP COLUMN role_id")
    for name in (
        "configuration_events",
        "published_configurations",
        "configuration_drafts",
        "role_permissions",
        "tenant_roles",
    ):
        op.execute(f"DROP TABLE {name}")
