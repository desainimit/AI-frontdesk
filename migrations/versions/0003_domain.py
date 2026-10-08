"""Tenant-scoped domain entities and database-enforced booking exclusions.

Revision ID: 0003_domain
Revises: 0002_multitenancy
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ExcludeConstraint

revision: str = "0003_domain"
down_revision: str | None = "0002_multitenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _common() -> list[sa.Column[UUID] | sa.Column[datetime]]:
    return [
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _times() -> list[sa.Column[datetime]]:
    return [
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "locations",
        *_common(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_locations_tenant_id_id"),
    )
    op.create_table(
        "providers",
        *_common(),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("specialty", sa.String(200), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_providers_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "location_id"], ["locations.tenant_id", "locations.id"]
        ),
    )
    op.create_table(
        "patients",
        *_common(),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(16), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_patients_tenant_id_id"),
    )
    op.create_table(
        "availability",
        *_common(),
        *_times(),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("slot_minutes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_id"], ["providers.tenant_id", "providers.id"]
        ),
        sa.CheckConstraint("ends_at > starts_at", name="positive_window"),
        sa.CheckConstraint("ends_at <= starts_at + interval '24 hours'", name="bounded_window"),
        sa.CheckConstraint("slot_minutes BETWEEN 5 AND 240", name="slot_minutes_range"),
        sa.CheckConstraint(
            "EXTRACT(EPOCH FROM (ends_at - starts_at)) >= slot_minutes * 60", name="fits_slot"
        ),
        ExcludeConstraint(
            ("tenant_id", "="),
            ("provider_id", "="),
            (sa.text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            name="ex_availability_provider_overlap",
            using="gist",
        ),
    )
    op.create_table(
        "appointments",
        *_common(),
        *_times(),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_id"], ["providers.tenant_id", "providers.id"]
        ),
        sa.ForeignKeyConstraint(["tenant_id", "patient_id"], ["patients.tenant_id", "patients.id"]),
        sa.CheckConstraint("ends_at > starts_at", name="positive_duration"),
        sa.CheckConstraint("status IN ('booked', 'cancelled')", name="status_allowed"),
        sa.CheckConstraint("version >= 1", name="positive_version"),
        ExcludeConstraint(
            ("tenant_id", "="),
            ("provider_id", "="),
            (sa.text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            where=sa.text("status = 'booked'"),
            name="ex_appointments_provider_overlap",
            using="gist",
        ),
        ExcludeConstraint(
            ("tenant_id", "="),
            ("patient_id", "="),
            (sa.text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            where=sa.text("status = 'booked'"),
            name="ex_appointments_patient_overlap",
            using="gist",
        ),
    )
    for table in ("locations", "providers", "patients", "availability", "appointments"):
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])
        predicate = "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    for table in ("appointments", "availability", "patients", "providers", "locations"):
        op.drop_table(table)
    # btree_gist may be shared with other schemas; never drop the extension automatically.
