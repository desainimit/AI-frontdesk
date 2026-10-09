from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin


class PlatformAuditEvent(IdentityMixin, Base):
    """Account and platform activity: sign-ins, security changes, workspace visits.

    Append-only. The API login can add rows but cannot read them back.
    """

    __tablename__ = "platform_audit_events"
    # The account that acted.
    user_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    action: Mapped[str] = mapped_column(String(80))
    request_id: Mapped[UUID]
    resource_id: Mapped[UUID | None]
    tenant_id: Mapped[UUID | None] = mapped_column(index=True)
    support_session_id: Mapped[UUID | None] = mapped_column(index=True)
    http_method: Mapped[str | None] = mapped_column(String(10))
    route: Mapped[str | None] = mapped_column(String(200))
