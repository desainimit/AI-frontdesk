"""Direct-save clinic settings and the clinic schedule model.

Replaces draft/publish configuration, the generic business directory and one-off
availability windows with: one settings row per clinic, weekly provider schedules,
time off and appointment types. Starts from an empty database.

Revision ID: 0003_clinic_settings
Revises: 0002_single_identity
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0003_clinic_settings"
down_revision: str | None = "0002_single_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
# Dependants first.
OLD_TABLES = (
    "configuration_events",
    "published_configurations",
    "configuration_drafts",
    "configuration_resources",
    "business_holidays",
    "business_opening_windows",
    "business_hours_policies",
    "business_directory_mutations",
    "business_service_resources",
    "business_service_staff",
    "business_staff_holidays",
    "business_staff_windows",
    "business_resources",
    "business_services",
    "business_staff",
    "availability",
)
NEW_TABLES = (
    "tenant_settings",
    "settings_changes",
    "services",
    "provider_schedules",
    "provider_time_off",
)


def tenant_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade() -> None:
    if not context.is_offline_mode() and op.get_bind().scalar(
        sa.text("SELECT 1 FROM tenants LIMIT 1")
    ):
        raise RuntimeError(
            "This upgrade starts from an empty database: it does not carry clinic settings "
            "over from the old tables. Create a new database and run the setup job."
        )

    for table in OLD_TABLES:
        op.execute(f"DROP TABLE {table} CASCADE")
    for column in ("configuration_version", "agent_id", "channel_id"):
        op.drop_column("calls", column)
    op.drop_constraint(op.f("ck_tenants_human_transfer_e164"), "tenants", type_="check")
    for column in (
        "language_policy",
        "human_transfer_number",
        "verification_policy",
        "requires_saas_activation",
    ):
        op.drop_column("tenants", column)

    op.create_table(
        "tenant_settings",
        *tenant_columns(),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_settings")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tenant_settings_tenant_id_tenants")
        ),
        sa.UniqueConstraint("tenant_id", name=op.f("uq_tenant_settings_tenant_id")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_tenant_settings_version_positive")),
    )
    op.create_table(
        "settings_changes",
        *tenant_columns(),
        sa.Column("section", sa.String(30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changed_by", sa.String(200), nullable=False),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("changes", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settings_changes")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_settings_changes_tenant_id_tenants")
        ),
    )
    op.create_table(
        "services",
        *tenant_columns(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000), server_default="", nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_services_tenant_id_tenants")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_services_tenant_id_id")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_services_name")),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 5 AND 240", name=op.f("ck_services_duration_range")
        ),
    )
    op.create_table(
        "provider_schedules",
        *tenant_columns(),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("starts", sa.Time(), nullable=False),
        sa.Column("ends", sa.Time(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_schedules")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_provider_schedules_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["providers.tenant_id", "providers.id"],
            name=op.f("fk_provider_schedules_provider"),
        ),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name=op.f("ck_provider_schedules_weekday")),
        sa.CheckConstraint("starts < ends", name=op.f("ck_provider_schedules_ordered_window")),
    )
    op.create_table(
        "provider_time_off",
        *tenant_columns(),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(200), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_time_off")),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_provider_time_off_tenant_id_tenants")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["providers.tenant_id", "providers.id"],
            name=op.f("fk_provider_time_off_provider"),
        ),
        sa.CheckConstraint("ends_on >= starts_on", name=op.f("ck_provider_time_off_ordered_days")),
    )
    for table in NEW_TABLES:
        op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )

    op.add_column(
        "locations", sa.Column("address", sa.String(1000), server_default="", nullable=False)
    )
    op.add_column("locations", sa.Column("phone", sa.String(20), server_default="", nullable=False))
    op.add_column(
        "providers", sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.add_column("patients", sa.Column("email", sa.String(254)))
    op.add_column("patients", sa.Column("language", sa.String(10)))
    op.add_column(
        "patients",
        sa.Column("sms_opt_out", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("appointments", sa.Column("service_id", sa.Uuid()))
    op.create_foreign_key(
        op.f("fk_appointments_service"),
        "appointments",
        "services",
        ["tenant_id", "service_id"],
        ["tenant_id", "id"],
    )

    # A membership's access now comes only from its role.
    op.drop_constraint(op.f("ck_users_role_allowed"), "users", type_="check")
    op.drop_column("users", "role")
    op.alter_column("users", "role_id", nullable=False)


def downgrade() -> None:
    raise RuntimeError("Recreate the database instead: this revision drops the old model.")
