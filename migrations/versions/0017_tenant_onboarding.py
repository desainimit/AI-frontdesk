"""Platform-only onboarding drafts and pending administrator provisioning.

Revision ID: 0017_tenant_onboarding
Revises: 0016_support_writes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_tenant_onboarding"
down_revision = "0016_support_writes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_onboardings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("admin_id", sa.Uuid(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("creator_id", sa.Uuid(), sa.ForeignKey("platform_users.id"), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("draft", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_onboardings_tenant_id"),
        sa.UniqueConstraint("creator_id", "key_digest", name="uq_tenant_onboardings_creation_key"),
        sa.ForeignKeyConstraint(["tenant_id", "admin_id"], ["users.tenant_id", "users.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"], ["locations.tenant_id", "locations.id"]
        ),
        sa.CheckConstraint("revision >= 1", name="positive_revision"),
        sa.CheckConstraint("status IN ('draft', 'invited')", name="onboarding_status"),
    )
    # Ordinary tenant/support context cannot read onboarding or its creation keys.
    predicate = """EXISTS (SELECT 1 FROM public.platform_users p
        WHERE p.id = nullif(current_setting('app.platform_user_id', true), '')::uuid
        AND p.is_active AND p.role = 'super_admin')"""
    op.execute("ALTER TABLE tenant_onboardings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_onboardings FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY platform_onboarding_read ON tenant_onboardings FOR SELECT "
        f"USING ({predicate})"
    )
    op.execute(
        "CREATE POLICY platform_onboarding_create ON tenant_onboardings FOR INSERT "
        f"WITH CHECK ({predicate} AND "
        "creator_id = nullif(current_setting('app.platform_user_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY platform_onboarding_update ON tenant_onboardings FOR UPDATE "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def downgrade() -> None:
    op.drop_table("tenant_onboardings")
