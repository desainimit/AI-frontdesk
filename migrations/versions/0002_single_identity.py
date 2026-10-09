"""One account and one session for everyone.

Replaces platform users, tenant password identities, API tokens and their four session
types with `accounts`, `account_mfa` and `sessions`. Starts from an empty database:
nothing is copied from the old tables.

Revision ID: 0002_single_identity
Revises: 0001_baseline
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0002_single_identity"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A platform administrator, named by the transaction-local setting the API sets.
PLATFORM_ADMIN = (
    "EXISTS (SELECT 1 FROM accounts a WHERE a.id = "
    "NULLIF(current_setting('app.platform_user_id', true), '')::uuid "
    "AND a.is_active AND a.is_platform_admin)"
)
ACCOUNT = "NULLIF(current_setting('app.account_id', true), '')::uuid"
PLATFORM_POLICIES = (
    ("platform_onboarding_create", "tenant_onboardings"),
    ("platform_onboarding_read", "tenant_onboardings"),
    ("platform_onboarding_update", "tenant_onboardings"),
    ("platform_phone_management", "phone_numbers"),
    ("platform_tenant_management", "tenants"),
)
# Dependants first.
OLD_TABLES = (
    "support_sessions",
    "platform_authenticators",
    "platform_sessions",
    "identity_sessions",
    "identity_memberships",
    "human_sessions",
    "human_accounts",
    "human_identities",
    "api_credentials",
    "platform_users",
)


def upgrade() -> None:
    if not context.is_offline_mode() and op.get_bind().scalar(
        sa.text("SELECT 1 FROM users LIMIT 1")
    ):
        raise RuntimeError(
            "This upgrade starts from an empty database: it does not carry sign-in data "
            "over from the old tables. Create a new database and run the setup job."
        )

    op.create_table(
        "accounts",
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=True),
        sa.Column("credential_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "is_platform_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "credential_revision >= 1", name=op.f("ck_accounts_credential_revision_positive")
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_accounts_normalized_email")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_accounts")),
        sa.UniqueConstraint("email", name=op.f("uq_accounts_email")),
    )
    op.create_table(
        "account_mfa",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_counter", sa.BigInteger(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recovery_digests",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("recovery_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(recovery_digests) = 'array' "
            "AND jsonb_array_length(recovery_digests) <= 8",
            name=op.f("ck_account_mfa_recovery_codes_bounded"),
        ),
        sa.CheckConstraint(
            "recovery_revision >= 0", name=op.f("ck_account_mfa_recovery_revision_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_account_mfa_account_id_accounts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_mfa")),
        sa.UniqueConstraint("account_id", name=op.f("uq_account_mfa_account_id")),
    )
    op.create_table(
        "sessions",
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mfa_pending", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("mfa_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("support_id", sa.Uuid(), nullable=True),
        sa.Column("support_reason", sa.String(length=500), nullable=True),
        sa.Column("support_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name=op.f("fk_sessions_account_id_accounts")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_sessions_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
    )
    op.create_index(op.f("ix_sessions_account_id"), "sessions", ["account_id"], unique=False)
    op.create_index(op.f("ix_sessions_tenant_id"), "sessions", ["tenant_id"], unique=False)

    # Everything that points at the old tables goes before they do.
    for policy, table in PLATFORM_POLICIES:
        op.execute(f"DROP POLICY {policy} ON {table}")
    op.drop_constraint("fk_audit_events_platform_user_id", "audit_events", type_="foreignkey")
    op.drop_constraint("fk_idempotency_platform_user", "idempotency_records", type_="foreignkey")
    op.drop_constraint("fk_idempotency_support_session", "idempotency_records", type_="foreignkey")
    op.drop_constraint(
        "fk_platform_audit_events_user_id_platform_users",
        "platform_audit_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_tenant_onboardings_creator_id_platform_users", "tenant_onboardings", type_="foreignkey"
    )
    op.execute("DROP TRIGGER accepted_identity_membership ON human_accounts")
    op.execute("DROP FUNCTION index_accepted_membership()")
    for table in OLD_TABLES:
        op.execute(f"DROP TABLE {table}")

    op.create_foreign_key(
        op.f("fk_audit_events_platform_user_id_accounts"),
        "audit_events",
        "accounts",
        ["platform_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_idempotency_records_platform_user_id_accounts"),
        "idempotency_records",
        "accounts",
        ["platform_user_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_platform_audit_events_user_id_accounts"),
        "platform_audit_events",
        "accounts",
        ["user_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_tenant_onboardings_creator_id_accounts"),
        "tenant_onboardings",
        "accounts",
        ["creator_id"],
        ["id"],
    )

    # Invitation and reset links belong to an account; a reset has no workspace, so these
    # two tables are no longer workspace data under row-level security.
    for table in ("auth_challenges", "auth_mail"):
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"DROP POLICY IF EXISTS auth_mail_delivery ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index(op.f("ix_auth_mail_tenant_id"), table_name="auth_mail")
    op.drop_constraint(
        op.f("fk_auth_mail_tenant_id_auth_challenges"), "auth_mail", type_="foreignkey"
    )
    op.drop_column("auth_mail", "tenant_id")
    op.add_column("auth_challenges", sa.Column("account_id", sa.Uuid(), nullable=False))
    op.alter_column("auth_challenges", "tenant_id", existing_type=sa.UUID(), nullable=True)
    op.alter_column("auth_challenges", "user_id", existing_type=sa.UUID(), nullable=True)
    op.drop_index(op.f("ix_auth_challenges_tenant_id"), table_name="auth_challenges")
    op.drop_constraint(op.f("uq_auth_challenges_tenant_id_id"), "auth_challenges", type_="unique")
    op.drop_constraint(op.f("uq_auth_challenges_key"), "auth_challenges", type_="unique")
    op.create_unique_constraint("uq_auth_challenges_key", "auth_challenges", ["key_digest"])
    op.create_index(
        op.f("ix_auth_challenges_account_id"), "auth_challenges", ["account_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_auth_challenges_account_id_accounts"),
        "auth_challenges",
        "accounts",
        ["account_id"],
        ["id"],
    )
    op.drop_column("auth_challenges", "attempts")
    op.create_check_constraint(
        op.f("ck_auth_challenges_invitation_membership"),
        "auth_challenges",
        "purpose <> 'invitation' OR (tenant_id IS NOT NULL AND user_id IS NOT NULL)",
    )
    op.create_foreign_key(
        op.f("fk_auth_mail_challenge_id_auth_challenges"),
        "auth_mail",
        "auth_challenges",
        ["challenge_id"],
        ["id"],
    )

    op.add_column("users", sa.Column("account_id", sa.Uuid(), nullable=False))
    op.add_column("users", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_users_account_id"), "users", ["account_id"], unique=False)
    op.create_unique_constraint(
        "uq_users_tenant_id_account_id", "users", ["tenant_id", "account_id"]
    )
    op.create_foreign_key(
        op.f("fk_users_account_id_accounts"), "users", "accounts", ["account_id"], ["id"]
    )

    # Platform administrators are now accounts.
    op.execute(
        f"CREATE POLICY platform_onboarding_create ON tenant_onboardings FOR INSERT "
        f"WITH CHECK ({PLATFORM_ADMIN} AND creator_id = "
        "NULLIF(current_setting('app.platform_user_id', true), '')::uuid)"
    )
    op.execute(
        "CREATE POLICY platform_onboarding_read ON tenant_onboardings FOR SELECT "
        f"USING ({PLATFORM_ADMIN})"
    )
    op.execute(
        f"CREATE POLICY platform_onboarding_update ON tenant_onboardings FOR UPDATE "
        f"USING ({PLATFORM_ADMIN}) WITH CHECK ({PLATFORM_ADMIN})"
    )
    for policy, table in PLATFORM_POLICIES[3:]:
        op.execute(
            f"CREATE POLICY {policy} ON {table} "
            f"USING ({PLATFORM_ADMIN}) WITH CHECK ({PLATFORM_ADMIN})"
        )
    # A signed-in account may read its own memberships and the workspaces they are in,
    # which is how the workspace list is built without naming a workspace first.
    op.execute(
        f"CREATE POLICY account_memberships ON users FOR SELECT USING (account_id = {ACCOUNT})"
    )
    op.execute(
        "CREATE POLICY account_workspaces ON tenants FOR SELECT USING (EXISTS ("
        f"SELECT 1 FROM users u WHERE u.tenant_id = tenants.id AND u.account_id = {ACCOUNT}))"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0002_single_identity drops the old sign-in tables and cannot be reversed. "
        "Restore the database from a backup taken before the upgrade."
    )
