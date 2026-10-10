from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin, TimestampMixin


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


class PlatformConnection(IdentityMixin, TimestampMixin, Base):
    """A provider account the platform owns: every clinic without its own uses this one.

    One row per purpose. The secret is stored only as AES-GCM ciphertext.
    """

    __tablename__ = "platform_connections"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('telephony', 'stt', 'tts', 'llm', 'email')", name="purpose_allowed"
        ),
        CheckConstraint(
            "status IN ('saved_unverified', 'verified', 'invalid')", name="status_allowed"
        ),
    )
    purpose: Mapped[str] = mapped_column(String(20), unique=True)
    provider: Mapped[str] = mapped_column(String(30))
    account_identifier: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    # Last characters of the secret, so an administrator can tell which one is saved.
    display_suffix: Mapped[str] = mapped_column(String(8), default="", server_default="")
    status: Mapped[str] = mapped_column(String(20), default="saved_unverified")
    error_code: Mapped[str | None] = mapped_column(String(40))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("accounts.id"))


class PlatformSettings(TimestampMixin, Base):
    """Product-wide choices of the platform administrators. Always exactly one row."""

    __tablename__ = "platform_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("session_minutes BETWEEN 5 AND 720", name="session_minutes_range"),
    )
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    product_name: Mapped[str] = mapped_column(String(100), default="AI Frontdesk")
    support_email: Mapped[str] = mapped_column(String(254), default="", server_default="")
    # AI models a clinic's own connection may choose from.
    allowed_llm_models: Mapped[list[str]] = mapped_column(JSONB)
    # How long a platform administrator stays signed in.
    session_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    # What a new clinic starts with; empty while the product's own defaults are in use.
    workspace_defaults: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class WorkspaceCreation(IdentityMixin, TimestampMixin, Base):
    """Who created a clinic and with which request, so a retried request creates nothing new.

    Also remembers the clinic's first owner and location. The table keeps its original
    name.
    """

    __tablename__ = "tenant_onboardings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_onboardings_tenant_id"),
        UniqueConstraint("creator_id", "key_digest", name="uq_tenant_onboardings_creation_key"),
        ForeignKeyConstraint(["tenant_id", "admin_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(["tenant_id", "location_id"], ["locations.tenant_id", "locations.id"]),
    )

    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    admin_id: Mapped[UUID]
    location_id: Mapped[UUID]
    creator_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    key_digest: Mapped[str] = mapped_column(String(64))
    request_digest: Mapped[str] = mapped_column(String(64))
