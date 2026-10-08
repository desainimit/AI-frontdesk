"""Tenant password accounts, revocable human sessions, challenges and auth mail.

Revision ID: 0018_human_auth
Revises: 0017_tenant_onboarding
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0018_human_auth"
down_revision: str | None = "0017_tenant_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("human_accounts", "human_sessions", "auth_challenges", "auth_mail")


def _identity() -> list[sa.Column[UUID] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _membership(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", "user_id"],
        ["users.tenant_id", "users.id"],
        name=op.f(f"fk_{table}_tenant_id_users"),
    )


def upgrade() -> None:
    op.create_table(
        "auth_challenges",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        *_identity(),
        sa.CheckConstraint(
            "purpose IN ('invitation', 'reset')",
            name=op.f("ck_auth_challenges_challenge_purpose"),
        ),
        sa.CheckConstraint(
            "attempts BETWEEN 0 AND 5", name=op.f("ck_auth_challenges_challenge_attempts")
        ),
        _membership("auth_challenges"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_challenges")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_auth_challenges_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "key_digest", name="uq_auth_challenges_key"),
    )
    op.create_index(
        op.f("ix_auth_challenges_tenant_id"), "auth_challenges", ["tenant_id"], unique=False
    )
    op.create_table(
        "human_accounts",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        *_identity(),
        _membership("human_accounts"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_human_accounts")),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_human_accounts_membership"),
    )
    op.create_table(
        "human_sessions",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_identity(),
        _membership("human_sessions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_human_sessions")),
    )
    op.create_index(
        op.f("ix_human_sessions_tenant_id"), "human_sessions", ["tenant_id"], unique=False
    )
    op.create_table(
        "auth_mail",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("challenge_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        *_identity(),
        sa.CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'cancelled')",
            name=op.f("ck_auth_mail_mail_status"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "challenge_id"],
            ["auth_challenges.tenant_id", "auth_challenges.id"],
            name=op.f("fk_auth_mail_tenant_id_auth_challenges"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_mail")),
        sa.UniqueConstraint("challenge_id", name="uq_auth_mail_challenge"),
    )
    op.create_index(op.f("ix_auth_mail_tenant_id"), "auth_mail", ["tenant_id"], unique=False)
    # Without forced RLS these tables would expose password hashes and session digests
    # across tenants, and the runtime role guard rejects the whole schema.
    predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_mail_tenant_id"), table_name="auth_mail")
    op.drop_table("auth_mail")
    op.drop_index(op.f("ix_human_sessions_tenant_id"), table_name="human_sessions")
    op.drop_table("human_sessions")
    op.drop_table("human_accounts")
    op.drop_index(op.f("ix_auth_challenges_tenant_id"), table_name="auth_challenges")
    op.drop_table("auth_challenges")
