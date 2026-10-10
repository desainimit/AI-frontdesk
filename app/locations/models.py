from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class Location(TenantRecord):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_locations_tenant_id_id"),)
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    phone: Mapped[str] = mapped_column(String(20), default="", server_default="")
