from uuid import UUID

from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class Provider(TenantRecord):
    __tablename__ = "providers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_providers_tenant_id_id"),
        ForeignKeyConstraint(["tenant_id", "location_id"], ["locations.tenant_id", "locations.id"]),
    )
    location_id: Mapped[UUID]
    display_name: Mapped[str] = mapped_column(String(200))
    specialty: Mapped[str | None] = mapped_column(String(200))
