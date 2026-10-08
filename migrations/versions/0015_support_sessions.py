"""Scoped, revocable platform support sessions and attributable audit."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_support_sessions"
down_revision = "0014_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "support_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "platform_user_id", sa.Uuid(), sa.ForeignKey("platform_users.id"), nullable=False
        ),
        sa.Column(
            "platform_session_id", sa.Uuid(), sa.ForeignKey("platform_sessions.id"), nullable=False
        ),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("capabilities", JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_support_sessions_tenant_id_id"),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'", name="support_capabilities_array"
        ),
    )
    op.create_index("ix_support_sessions_tenant_id", "support_sessions", ["tenant_id"])
    op.create_index(
        "ix_support_sessions_platform_user_id", "support_sessions", ["platform_user_id"]
    )
    op.execute("ALTER TABLE support_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE support_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON support_sessions "
        "USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.add_column("platform_audit_events", sa.Column("tenant_id", sa.Uuid()))
    op.add_column("platform_audit_events", sa.Column("support_session_id", sa.Uuid()))
    op.add_column("platform_audit_events", sa.Column("http_method", sa.String(10)))
    op.add_column("platform_audit_events", sa.Column("route", sa.String(200)))
    op.create_index("ix_platform_audit_events_tenant_id", "platform_audit_events", ["tenant_id"])
    op.create_index(
        "ix_platform_audit_events_support_session_id",
        "platform_audit_events",
        ["support_session_id"],
    )
    op.add_column("audit_events", sa.Column("platform_user_id", sa.Uuid()))
    op.add_column("audit_events", sa.Column("support_session_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_audit_events_platform_user_id",
        "audit_events",
        "platform_users",
        ["platform_user_id"],
        ["id"],
    )
    op.drop_constraint(op.f("ck_audit_events_actor_type_allowed"), "audit_events", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_events_actor_type_allowed"),
        "audit_events",
        "actor_type IN ('user', 'system', 'platform_support')",
    )
    op.create_check_constraint(
        op.f("ck_audit_events_support_actor_required"),
        "audit_events",
        "actor_type <> 'platform_support' OR "
        "(actor_id IS NULL AND platform_user_id IS NOT NULL AND support_session_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_audit_events_support_actor_required"), "audit_events", type_="check"
    )
    op.drop_constraint(op.f("ck_audit_events_actor_type_allowed"), "audit_events", type_="check")
    op.create_check_constraint(
        op.f("ck_audit_events_actor_type_allowed"),
        "audit_events",
        "actor_type IN ('user', 'system')",
    )
    op.drop_constraint("fk_audit_events_platform_user_id", "audit_events", type_="foreignkey")
    op.drop_column("audit_events", "support_session_id")
    op.drop_column("audit_events", "platform_user_id")
    op.drop_index("ix_platform_audit_events_support_session_id", "platform_audit_events")
    op.drop_index("ix_platform_audit_events_tenant_id", "platform_audit_events")
    for column in ("route", "http_method", "support_session_id", "tenant_id"):
        op.drop_column("platform_audit_events", column)
    op.drop_table("support_sessions")
