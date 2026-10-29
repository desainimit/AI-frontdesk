"""Several numbers per clinic, each for the whole clinic, a location or a provider.

Revision ID: 0015_number_assignment
Revises: 0014_model_usage
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_number_assignment"
down_revision: str | None = "0014_model_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT = "(NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid"


def upgrade() -> None:
    op.add_column(
        "phone_numbers", sa.Column("label", sa.String(length=60), server_default="", nullable=False)
    )
    op.add_column("phone_numbers", sa.Column("location_id", sa.Uuid(), nullable=True))
    op.add_column("phone_numbers", sa.Column("provider_id", sa.Uuid(), nullable=True))
    op.add_column(
        "phone_numbers",
        sa.Column("is_main", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    # Deleting a location or provider frees its number for the whole clinic again.
    op.execute(
        "ALTER TABLE phone_numbers ADD CONSTRAINT fk_phone_numbers_location "
        "FOREIGN KEY (tenant_id, location_id) REFERENCES locations (tenant_id, id) "
        "ON DELETE SET NULL (location_id)"
    )
    op.execute(
        "ALTER TABLE phone_numbers ADD CONSTRAINT fk_phone_numbers_provider "
        "FOREIGN KEY (tenant_id, provider_id) REFERENCES providers (tenant_id, id) "
        "ON DELETE SET NULL (provider_id)"
    )
    # A released number may serve another clinic; its old row keeps the call history.
    op.drop_constraint("uq_phone_numbers_number", "phone_numbers", type_="unique")
    op.create_index(
        "uq_phone_numbers_active_number",
        "phone_numbers",
        ["number"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "uq_phone_numbers_provider",
        "phone_numbers",
        ["tenant_id", "provider_id"],
        unique=True,
        postgresql_where=sa.text("provider_id IS NOT NULL AND is_active"),
    )
    op.create_index(
        "uq_phone_numbers_main",
        "phone_numbers",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_main AND is_active"),
    )
    # Each clinic's oldest active number becomes its main number.
    op.execute(
        "UPDATE phone_numbers SET is_main = true WHERE id IN ("
        "SELECT DISTINCT ON (tenant_id) id FROM phone_numbers WHERE is_active "
        "ORDER BY tenant_id, created_at)"
    )
    # Clinic administrators decide whose line each of their numbers is; which numbers
    # a clinic has stays with the platform (the service changes only these fields).
    op.execute(
        "CREATE POLICY tenant_phone_assign ON phone_numbers FOR UPDATE "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_phone_assign ON phone_numbers")
    op.drop_index("uq_phone_numbers_main", table_name="phone_numbers")
    op.drop_index("uq_phone_numbers_provider", table_name="phone_numbers")
    op.drop_index("uq_phone_numbers_active_number", table_name="phone_numbers")
    op.create_unique_constraint("uq_phone_numbers_number", "phone_numbers", ["number"])
    op.drop_constraint("fk_phone_numbers_provider", "phone_numbers", type_="foreignkey")
    op.drop_constraint("fk_phone_numbers_location", "phone_numbers", type_="foreignkey")
    op.drop_column("phone_numbers", "is_main")
    op.drop_column("phone_numbers", "provider_id")
    op.drop_column("phone_numbers", "location_id")
    op.drop_column("phone_numbers", "label")
