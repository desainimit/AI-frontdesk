from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import IdentityMixin, TimestampMixin


class PlatformUser(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "platform_users"
    __table_args__ = (CheckConstraint("role = 'super_admin'", name="role_allowed"),)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(20), default="super_admin")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true())


class PlatformSession(IdentityMixin, Base):
    __tablename__ = "platform_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("platform_users.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformAuthenticator(IdentityMixin, Base):
    __tablename__ = "platform_authenticators"
    __table_args__ = (
        CheckConstraint("recovery_revision >= 0", name="recovery_revision_nonnegative"),
        CheckConstraint(
            "jsonb_typeof(recovery_digests) = 'array' "
            "AND jsonb_array_length(recovery_digests) <= 8",
            name="recovery_codes_bounded",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("platform_users.id"), unique=True)
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_counter: Mapped[int] = mapped_column(BigInteger, default=-1)
    failed_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recovery_digests: Mapped[list[str]] = mapped_column(
        JSONB, default=list[str], server_default="[]"
    )
    recovery_revision: Mapped[int] = mapped_column(default=0, server_default="0")


class PlatformAuditEvent(IdentityMixin, Base):
    __tablename__ = "platform_audit_events"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("platform_users.id"), index=True)
    action: Mapped[str] = mapped_column(String(80))
    request_id: Mapped[UUID]
    resource_id: Mapped[UUID | None]
    tenant_id: Mapped[UUID | None] = mapped_column(index=True)
    support_session_id: Mapped[UUID | None] = mapped_column(index=True)
    http_method: Mapped[str | None] = mapped_column(String(10))
    route: Mapped[str | None] = mapped_column(String(200))


class SupportSession(IdentityMixin, Base):
    __tablename__ = "support_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        UniqueConstraint("tenant_id", "id", name="uq_support_sessions_tenant_id_id"),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    platform_user_id: Mapped[UUID] = mapped_column(ForeignKey("platform_users.id"), index=True)
    platform_session_id: Mapped[UUID] = mapped_column(ForeignKey("platform_sessions.id"))
    token_digest: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(500))
    capabilities: Mapped[list[str]] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
