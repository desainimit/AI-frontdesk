"""Platform super-admin identity, sessions, and append-only audit.

Revision ID: 0004_platform_admin
Revises: 0003_domain
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision = "0004_platform_admin"
down_revision = "0003_domain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity() -> list[sa.Column[UUID] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "platform_users",
        *_identity(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.UniqueConstraint("email"),
        sa.CheckConstraint("role = 'super_admin'", name="role_allowed"),
    )
    op.create_table(
        "platform_sessions",
        *_identity(),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("platform_users.id"), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "platform_audit_events",
        *_identity(),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("platform_users.id"), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
    )
    for table in ("platform_sessions", "platform_audit_events"):
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    op.execute(
        "CREATE TRIGGER platform_audit_no_changes BEFORE UPDATE OR DELETE "
        "ON platform_audit_events FOR EACH ROW EXECUTE FUNCTION public.reject_audit_mutation()"
    )
    op.execute(
        "CREATE TRIGGER platform_audit_no_truncate BEFORE TRUNCATE ON platform_audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION public.reject_audit_mutation()"
    )
    # Platform administration is restricted to tenant metadata. Clinical tables retain
    # their original tenant-only RLS policies; there is no blanket RLS bypass.
    predicate = """EXISTS (SELECT 1 FROM public.platform_users p
        WHERE p.id = nullif(current_setting('app.platform_user_id', true), '')::uuid
        AND p.is_active AND p.role = 'super_admin')"""
    op.execute(
        "CREATE POLICY platform_tenant_management ON tenants "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY platform_tenant_management ON tenants")
    op.drop_table("platform_audit_events")
    op.drop_table("platform_sessions")
    op.drop_table("platform_users")
