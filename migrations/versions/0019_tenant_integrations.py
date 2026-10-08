"""Tenant provider connections, encrypted credential versions and live-service state.

Revision ID: 0019_tenant_integrations
Revises: 0018_human_auth
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_tenant_integrations"
down_revision: str | None = "0018_human_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # Existing tenants already take calls with platform credentials: keep them live.
    # Every tenant created afterwards starts in setup and must be activated explicitly.
    op.add_column(
        "tenants",
        sa.Column("service_state", sa.String(20), server_default="live", nullable=False),
    )
    op.alter_column("tenants", "service_state", server_default="setup")
    op.create_check_constraint(
        op.f("ck_tenants_service_state_allowed"), "tenants", "service_state IN ('setup', 'live')"
    )
    op.create_table(
        "tenant_integrations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("slot", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("ownership", sa.String(10), nullable=False),
        sa.Column("account_identifier", sa.String(64), nullable=True),
        sa.Column(
            "config", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_integrations")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tenant_integrations_tenant_id_tenants")
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_integrations_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "purpose", "slot", name="uq_tenant_integrations_binding"),
        sa.CheckConstraint(
            "purpose IN ('telephony', 'stt', 'tts', 'llm', 'embeddings')",
            name=op.f("ck_tenant_integrations_purpose_allowed"),
        ),
        sa.CheckConstraint(
            "slot IN ('primary', 'fallback')", name=op.f("ck_tenant_integrations_slot_allowed")
        ),
        sa.CheckConstraint(
            "ownership IN ('byo', 'platform')",
            name=op.f("ck_tenant_integrations_ownership_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('not_configured', 'saved_unverified', 'verified', 'invalid', 'revoked')",
            name=op.f("ck_tenant_integrations_status_allowed"),
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_tenant_integrations_positive_revision")),
    )
    op.create_index(
        op.f("ix_tenant_integrations_tenant_id"), "tenant_integrations", ["tenant_id"], unique=False
    )
    op.create_table(
        "integration_credential_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("display_suffix", sa.String(8), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_platform_user_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_credential_versions")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "integration_id"],
            ["tenant_integrations.tenant_id", "tenant_integrations.id"],
            name="fk_integration_credential_versions_integration",
        ),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_integration_credential_versions_tenant_id_id"
        ),
        sa.UniqueConstraint(
            "integration_id", "version", name="uq_integration_credential_versions_n"
        ),
        sa.UniqueConstraint(
            "integration_id", "key_digest", name="uq_integration_credential_versions_key"
        ),
        sa.CheckConstraint(
            "state IN ('staged', 'verified', 'active', 'retired', 'invalid')",
            name=op.f("ck_integration_credential_versions_state_allowed"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_integration_credential_versions_positive_version")
        ),
        sa.CheckConstraint(
            "ciphertext IS NOT NULL OR state IN ('retired', 'invalid')",
            name=op.f("ck_integration_credential_versions_secret_present"),
        ),
        sa.CheckConstraint(
            "(created_by_user_id IS NULL) <> (created_by_platform_user_id IS NULL)",
            name=op.f("ck_integration_credential_versions_single_author"),
        ),
    )
    op.create_index(
        op.f("ix_integration_credential_versions_tenant_id"),
        "integration_credential_versions",
        ["tenant_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_tenant_integrations_active_version",
        "tenant_integrations",
        "integration_credential_versions",
        ["tenant_id", "active_version_id"],
        ["tenant_id", "id"],
    )
    for table in ("tenant_integrations", "integration_credential_versions"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT}) WITH CHECK ({TENANT})"
        )

    # A number and each call record which connection and credential version served them.
    op.add_column("phone_numbers", sa.Column("integration_id", sa.Uuid(), nullable=True))
    op.add_column("phone_numbers", sa.Column("provider_number_sid", sa.String(34), nullable=True))
    op.create_foreign_key(
        "fk_phone_numbers_integration",
        "phone_numbers",
        "tenant_integrations",
        ["tenant_id", "integration_id"],
        ["tenant_id", "id"],
    )
    op.create_check_constraint(
        op.f("ck_phone_numbers_provider_number_sid"),
        "phone_numbers",
        "provider_number_sid IS NULL OR provider_number_sid ~ '^PN[0-9a-fA-F]{32}$'",
    )
    # Tenant administrators may bind only numbers attached to one of their connections;
    # unbound legacy assignment stays a platform-only operation.
    op.execute(
        "CREATE POLICY tenant_phone_bind ON phone_numbers FOR INSERT "
        f"WITH CHECK ({TENANT} AND integration_id IS NOT NULL)"
    )
    op.execute(
        "CREATE POLICY tenant_phone_update ON phone_numbers FOR UPDATE "
        f"USING ({TENANT} AND integration_id IS NOT NULL) "
        f"WITH CHECK ({TENANT} AND integration_id IS NOT NULL)"
    )
    op.add_column("calls", sa.Column("integration_id", sa.Uuid(), nullable=True))
    op.add_column("calls", sa.Column("credential_version_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_calls_integration",
        "calls",
        "tenant_integrations",
        ["tenant_id", "integration_id"],
        ["tenant_id", "id"],
    )
    op.create_foreign_key(
        "fk_calls_credential_version",
        "calls",
        "integration_credential_versions",
        ["tenant_id", "credential_version_id"],
        ["tenant_id", "id"],
    )
    op.create_check_constraint(
        op.f("ck_calls_integration_binding"),
        "calls",
        "(integration_id IS NULL) = (credential_version_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_calls_integration_binding"), "calls", type_="check")
    op.drop_constraint("fk_calls_credential_version", "calls", type_="foreignkey")
    op.drop_constraint("fk_calls_integration", "calls", type_="foreignkey")
    op.drop_column("calls", "credential_version_id")
    op.drop_column("calls", "integration_id")
    op.execute("DROP POLICY tenant_phone_update ON phone_numbers")
    op.execute("DROP POLICY tenant_phone_bind ON phone_numbers")
    op.drop_constraint(op.f("ck_phone_numbers_provider_number_sid"), "phone_numbers", type_="check")
    op.drop_constraint("fk_phone_numbers_integration", "phone_numbers", type_="foreignkey")
    op.drop_column("phone_numbers", "provider_number_sid")
    op.drop_column("phone_numbers", "integration_id")
    op.drop_constraint(
        "fk_tenant_integrations_active_version", "tenant_integrations", type_="foreignkey"
    )
    op.drop_index(
        op.f("ix_integration_credential_versions_tenant_id"),
        table_name="integration_credential_versions",
    )
    op.drop_table("integration_credential_versions")
    op.drop_index(op.f("ix_tenant_integrations_tenant_id"), table_name="tenant_integrations")
    op.drop_table("tenant_integrations")
    op.drop_constraint(op.f("ck_tenants_service_state_allowed"), "tenants", type_="check")
    op.drop_column("tenants", "service_state")
