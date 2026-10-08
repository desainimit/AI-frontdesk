"""Derive section authority from existing grants without relying on role names."""

from collections.abc import Sequence

from alembic import op

revision: str = "0030_section_permissions"
down_revision: str | None = "0029_workspace_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen migration data. Existing explicit rows win over derived starting grants.
DERIVED = {
    "agent:read": "configuration:read",
    "agent:update": "configuration:update",
    "agent:publish": "configuration:publish",
    "channel:read": "configuration:read",
    "channel:update": "configuration:update",
    "channel:publish": "configuration:publish",
    "scheduling:read": "configuration:read",
    "scheduling:update": "configuration:update",
    "scheduling:publish": "configuration:publish",
    "privacy:read": "configuration:read",
    "privacy:update": "configuration:update",
    "privacy:publish": "configuration:publish",
    "notification:read": "configuration:read",
    "notification:update": "configuration:update",
    "notification:publish": "configuration:publish",
    "workflow:publish": "configuration:publish",
    "billing:publish": "configuration:publish",
    "tenant:publish": "configuration:publish",
}


def upgrade() -> None:
    values = ",".join(f"('{key}','{source}')" for key, source in DERIVED.items())
    op.execute(
        "WITH inserted AS (INSERT INTO role_permissions(id,tenant_id,role_id,key,allowed) "
        "SELECT gen_random_uuid(),r.tenant_id,r.id,v.key,coalesce(p.allowed,false) "
        f"FROM tenant_roles r CROSS JOIN (VALUES {values}) AS v(key,source) "
        "LEFT JOIN role_permissions p ON p.tenant_id=r.tenant_id "
        "AND p.role_id=r.id AND p.key=v.source "
        "ON CONFLICT (tenant_id,role_id,key) DO NOTHING RETURNING role_id,tenant_id) "
        "UPDATE tenant_roles r SET revision=revision+1,updated_at=now() "
        "WHERE EXISTS (SELECT 1 FROM inserted i WHERE i.role_id=r.id AND i.tenant_id=r.tenant_id)"
    )


def downgrade() -> None:
    raise RuntimeError("Permission catalog cutover is forward-only; restore a verified backup.")
