"""Tenant-owned provider connections and their immutable, encrypted credential versions.

Plaintext provider secrets never reach these rows: only AES-GCM ciphertext whose
authenticated context binds tenant, connection, provider, purpose and version.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.domain import TenantRecord
from app.core.database.types import IdentityMixin


class TenantIntegration(TenantRecord):
    __tablename__ = "tenant_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenant_integrations_tenant_id_id"),
        UniqueConstraint("tenant_id", "purpose", "slot", name="uq_tenant_integrations_binding"),
        ForeignKeyConstraint(
            ["tenant_id", "active_version_id"],
            ["integration_credential_versions.tenant_id", "integration_credential_versions.id"],
            name="fk_tenant_integrations_active_version",
            use_alter=True,
        ),
        CheckConstraint(
            "purpose IN ('telephony', 'stt', 'tts', 'llm', 'embeddings')", name="purpose_allowed"
        ),
        CheckConstraint("slot = 'primary'", name="slot_allowed"),
        CheckConstraint("ownership IN ('byo', 'platform')", name="ownership_allowed"),
        CheckConstraint(
            "status IN ('not_configured', 'saved_unverified', 'verified', 'invalid', 'revoked')",
            name="status_allowed",
        ),
        CheckConstraint("revision >= 1", name="positive_revision"),
    )

    purpose: Mapped[str] = mapped_column(String(20))
    slot: Mapped[str] = mapped_column(String(10), default="primary")
    provider: Mapped[str] = mapped_column(String(30))
    ownership: Mapped[str] = mapped_column(String(10), default="byo")
    account_identifier: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), default="not_configured")
    revision: Mapped[int] = mapped_column(default=1)
    active_version_id: Mapped[UUID | None]
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CredentialVersion(IdentityMixin, Base):
    __tablename__ = "integration_credential_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "integration_id"],
            ["tenant_integrations.tenant_id", "tenant_integrations.id"],
            name="fk_integration_credential_versions_integration",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_integration_credential_versions_tenant_id_id"),
        UniqueConstraint("integration_id", "version", name="uq_integration_credential_versions_n"),
        UniqueConstraint(
            "integration_id", "key_digest", name="uq_integration_credential_versions_key"
        ),
        CheckConstraint(
            "state IN ('staged', 'verified', 'active', 'retired', 'invalid')",
            name="state_allowed",
        ),
        CheckConstraint("version >= 1", name="positive_version"),
        # Usable versions must still hold their secret; purged ones can never be promoted.
        CheckConstraint(
            "ciphertext IS NOT NULL OR state IN ('retired', 'invalid')", name="secret_present"
        ),
        CheckConstraint(
            "(created_by_user_id IS NULL) <> (created_by_platform_user_id IS NULL)",
            name="single_author",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(index=True)
    integration_id: Mapped[UUID]
    version: Mapped[int]
    state: Mapped[str] = mapped_column(String(20), default="staged")
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    display_suffix: Mapped[str] = mapped_column(String(8))
    fingerprint: Mapped[str] = mapped_column(String(64))
    key_digest: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(40))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID | None]
    created_by_platform_user_id: Mapped[UUID | None]
