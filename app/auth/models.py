from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import IdentityMixin


class ApiCredential(IdentityMixin, Base):
    __tablename__ = "api_credentials"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    user_id: Mapped[UUID]
    token_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
