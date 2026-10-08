from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.domain import TenantRecord


class ConfigurationResource(TenantRecord):
    __tablename__ = "configuration_resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_configuration_resources_tenant_id_id"),
        UniqueConstraint("tenant_id", "kind", "name", name="uq_configuration_resources_name"),
        CheckConstraint("kind IN ('agent', 'channel')", name="resource_kind"),
        CheckConstraint(
            "(kind = 'agent' AND channel_kind IS NULL) OR "
            "(kind = 'channel' AND channel_kind IS NOT NULL AND channel_kind IN ('voice', 'chat'))",
            name="channel_kind",
        ),
        CheckConstraint("revision >= 1", name="resource_revision"),
    )
    kind: Mapped[str] = mapped_column(String(20))
    channel_kind: Mapped[str | None] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(default=True)
    creation_hash: Mapped[str] = mapped_column(String(64))


class ConfigurationDraft(TenantRecord):
    __tablename__ = "configuration_drafts"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_configuration_drafts_tenant"),)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    defaults_version: Mapped[int] = mapped_column(Integer, default=1)
    published_version: Mapped[int | None]


class PublishedConfiguration(TenantRecord):
    __tablename__ = "published_configurations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_published_configurations_version"),
        UniqueConstraint("tenant_id", "id", name="uq_published_configurations_tenant_id_id"),
    )
    version: Mapped[int] = mapped_column(Integer)
    source_revision: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    published_by: Mapped[UUID | None]
    platform_user_id: Mapped[UUID | None]


class ConfigurationEvent(TenantRecord):
    """Durable invalidation event; publication and this event commit together."""

    __tablename__ = "configuration_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "configuration_id", name="uq_configuration_events_publication"
        ),
        Index(
            "ix_configuration_events_pending",
            "available_at",
            "created_at",
            "version",
            postgresql_where=text("delivered_at IS NULL"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "configuration_id"],
            ["published_configurations.tenant_id", "published_configurations.id"],
        ),
    )
    configuration_id: Mapped[UUID]
    version: Mapped[int] = mapped_column(Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
