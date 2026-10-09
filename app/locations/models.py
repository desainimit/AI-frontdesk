from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class Location(TenantRecord):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_locations_tenant_id_id"),)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
