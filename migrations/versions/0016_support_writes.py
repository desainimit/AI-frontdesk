"""Attribute tenant idempotency records to scoped platform support sessions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0016_support_writes"
down_revision = "0015_support_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRINCIPAL_CHECK = (
    "(actor_id IS NOT NULL AND call_id IS NULL AND support_session_id IS NULL "
    "AND platform_user_id IS NULL) OR "
    "(call_id IS NOT NULL AND actor_id IS NULL AND support_session_id IS NULL "
    "AND platform_user_id IS NULL) OR "
    "(support_session_id IS NOT NULL AND platform_user_id IS NOT NULL "
    "AND actor_id IS NULL AND call_id IS NULL)"
)


def upgrade() -> None:
    op.add_column("idempotency_records", sa.Column("platform_user_id", sa.Uuid()))
    op.add_column("idempotency_records", sa.Column("support_session_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_idempotency_platform_user",
        "idempotency_records",
        "platform_users",
        ["platform_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_idempotency_support_session",
        "idempotency_records",
        "support_sessions",
        ["tenant_id", "support_session_id"],
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_idempotency_support_key",
        "idempotency_records",
        ["tenant_id", "support_session_id", "key_digest"],
    )
    op.drop_constraint("idempotency_actor_or_call", "idempotency_records", type_="check")
    op.create_check_constraint(
        "idempotency_principal_exactly_one", "idempotency_records", PRINCIPAL_CHECK
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM idempotency_records "
                "WHERE support_session_id IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Cannot downgrade while support idempotency records exist")
    op.drop_constraint("idempotency_principal_exactly_one", "idempotency_records", type_="check")
    op.create_check_constraint(
        "idempotency_actor_or_call",
        "idempotency_records",
        "(actor_id IS NULL) <> (call_id IS NULL)",
    )
    op.drop_constraint("uq_idempotency_support_key", "idempotency_records", type_="unique")
    op.drop_constraint("fk_idempotency_support_session", "idempotency_records", type_="foreignkey")
    op.drop_constraint("fk_idempotency_platform_user", "idempotency_records", type_="foreignkey")
    op.drop_column("idempotency_records", "support_session_id")
    op.drop_column("idempotency_records", "platform_user_id")
