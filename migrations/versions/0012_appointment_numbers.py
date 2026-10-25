"""A number for each appointment that patients and staff can quote: A-1, A-2, ... per clinic.

Revision ID: 0012_appointment_numbers
Revises: 0011_turn_audio
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_appointment_numbers"
down_revision: str | None = "0011_turn_audio"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # The last number each clinic gave out. A counter rather than "highest + 1": a caller
    # booking for themselves can only see their own appointments.
    op.create_table(
        "appointment_counters",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("last", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_appointment_counters_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("tenant_id", name=op.f("pk_appointment_counters")),
    )
    op.execute("ALTER TABLE appointment_counters ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE appointment_counters FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON appointment_counters "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )
    # Appointments made before this get numbers in the order they were booked.
    op.add_column("appointments", sa.Column("number", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE appointments SET number = numbered.number FROM ("
        "SELECT id, row_number() OVER (PARTITION BY tenant_id ORDER BY created_at, id) AS number "
        "FROM appointments) AS numbered WHERE appointments.id = numbered.id"
    )
    op.execute(
        "INSERT INTO appointment_counters (tenant_id, last) "
        "SELECT tenant_id, max(number) FROM appointments GROUP BY tenant_id"
    )
    op.alter_column("appointments", "number", nullable=False)
    op.create_unique_constraint(
        op.f("uq_appointments_number"), "appointments", ["tenant_id", "number"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_appointments_number"), "appointments", type_="unique")
    op.drop_column("appointments", "number")
    op.execute("DROP POLICY tenant_isolation ON appointment_counters")
    op.drop_table("appointment_counters")
