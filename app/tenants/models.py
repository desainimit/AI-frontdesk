from sqlalchemy import Boolean, CheckConstraint, String, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin, TimestampMixin


class Tenant(IdentityMixin, TimestampMixin, Base):
    """A clinic's workspace. How the clinic works is in `tenant_settings`."""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("service_state IN ('setup', 'live')", name="service_state_allowed"),
    )

    # Kept equal to the clinic name in settings; lists and emails read it from here.
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Independent of is_active: a clinic in setup can be managed but takes no calls.
    service_state: Mapped[str] = mapped_column(String(20), default="setup", server_default="setup")
