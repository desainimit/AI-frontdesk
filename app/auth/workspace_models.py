"""Identity admission records are separate from tenant authorization."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin


class IdentityMembership(IdentityMixin, Base):
    __tablename__ = "identity_memberships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "identity_id"],
            ["human_accounts.tenant_id", "human_accounts.user_id", "human_accounts.identity_id"],
        ),
        UniqueConstraint("tenant_id", "user_id", name="uq_identity_memberships_member"),
    )
    identity_id: Mapped[UUID] = mapped_column(ForeignKey("human_identities.id"), index=True)
    tenant_id: Mapped[UUID]
    user_id: Mapped[UUID]


class IdentitySession(IdentityMixin, Base):
    __tablename__ = "identity_sessions"
    identity_id: Mapped[UUID] = mapped_column(ForeignKey("human_identities.id"), index=True)
    credential_revision: Mapped[int]
    token_digest: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[UUID]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
