"""Whether each text was delivered, and sending through a clinic's Messaging Service.

Revision ID: 0016_message_delivery
Revises: 0015_number_assignment
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_message_delivery"
down_revision: str | None = "0015_number_assignment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbound_messages", sa.Column("provider_message_id", sa.String(length=40), nullable=True)
    )
    op.add_column(
        "outbound_messages", sa.Column("delivery_status", sa.String(length=12), nullable=True)
    )
    op.add_column(
        "outbound_messages",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbound_messages", sa.Column("messaging_service", sa.String(length=34), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("outbound_messages", "messaging_service")
    op.drop_column("outbound_messages", "delivered_at")
    op.drop_column("outbound_messages", "delivery_status")
    op.drop_column("outbound_messages", "provider_message_id")
