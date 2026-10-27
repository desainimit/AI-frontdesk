"""How fast the receptionist speaks on calls, chosen by the platform administrators.

Revision ID: 0013_speaking_speed
Revises: 0012_appointment_numbers
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_speaking_speed"
down_revision: str | None = "0012_appointment_numbers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column("speaking_speed", sa.String(length=10), server_default="normal", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_platform_settings_speaking_speed_choice"),
        "platform_settings",
        "speaking_speed IN ('slow', 'normal', 'fast', 'very_fast')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_platform_settings_speaking_speed_choice"), "platform_settings", type_="check"
    )
    op.drop_column("platform_settings", "speaking_speed")
