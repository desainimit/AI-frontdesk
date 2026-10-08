"""Call-bound patient access and call-scoped idempotency."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0007_appointment_agent"
down_revision = "0006_telephony"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint("uq_calls_tenant_id_id", "calls", ["tenant_id", "id"])
    for name in ("access_patient_id", "verified_patient_id"):
        op.add_column("calls", sa.Column(name, sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_calls_{name}_patients",
            "calls",
            "patients",
            ["tenant_id", name],
            ["tenant_id", "id"],
        )
    op.add_column("calls", sa.Column("access_digest", sa.String(128), nullable=True))
    op.add_column("calls", sa.Column("access_salt", sa.String(32), nullable=True))
    op.add_column(
        "calls", sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "calls", sa.Column("access_attempts", sa.Integer(), server_default="0", nullable=False)
    )
    op.create_check_constraint(
        "access_attempts_bounded", "calls", "access_attempts BETWEEN 0 AND 3"
    )
    op.alter_column("idempotency_records", "actor_id", nullable=True)
    op.add_column("idempotency_records", sa.Column("call_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_idempotency_call",
        "idempotency_records",
        "calls",
        ["tenant_id", "call_id"],
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_idempotency_call_key", "idempotency_records", ["tenant_id", "call_id", "key_digest"]
    )
    op.create_check_constraint(
        "idempotency_actor_or_call",
        "idempotency_records",
        "(actor_id IS NULL) <> (call_id IS NULL)",
    )


def downgrade() -> None:
    # Call responses cannot be represented by the older user-only schema.
    op.execute("DELETE FROM idempotency_records WHERE call_id IS NOT NULL")
    op.drop_constraint("idempotency_actor_or_call", "idempotency_records", type_="check")
    op.drop_constraint("uq_idempotency_call_key", "idempotency_records", type_="unique")
    op.drop_constraint("fk_idempotency_call", "idempotency_records", type_="foreignkey")
    op.drop_column("idempotency_records", "call_id")
    op.alter_column("idempotency_records", "actor_id", nullable=False)
    op.drop_constraint("access_attempts_bounded", "calls", type_="check")
    for name in ("access_patient_id", "verified_patient_id"):
        op.drop_constraint(f"fk_calls_{name}_patients", "calls", type_="foreignkey")
    for name in (
        "access_patient_id",
        "verified_patient_id",
        "access_digest",
        "access_salt",
        "access_expires_at",
        "access_attempts",
    ):
        op.drop_column("calls", name)
    op.drop_constraint("uq_calls_tenant_id_id", "calls", type_="unique")
