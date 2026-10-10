from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class TenantSettings(TenantRecord):
    """One row per clinic. `version` goes up by one on every save."""

    __tablename__ = "tenant_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_settings_tenant_id"),
        CheckConstraint("version >= 1", name="version_positive"),
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)


class SettingsChange(TenantRecord):
    """What one save changed: the settings history a clinic can read."""

    __tablename__ = "settings_changes"
    section: Mapped[str] = mapped_column(String(30))
    version: Mapped[int]
    # Display name kept as text so the history survives a member being removed.
    changed_by: Mapped[str] = mapped_column(String(200))
    actor_id: Mapped[UUID | None]
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
