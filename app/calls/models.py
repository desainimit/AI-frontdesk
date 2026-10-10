from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class PhoneNumber(TenantRecord):
    __tablename__ = "phone_numbers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_phone_numbers_tenant_id_id"),
        CheckConstraint("number ~ '^\\+[1-9][0-9]{7,14}$'", name="e164"),
        ForeignKeyConstraint(
            ["tenant_id", "integration_id"],
            ["tenant_integrations.tenant_id", "tenant_integrations.id"],
            name="fk_phone_numbers_integration",
        ),
        CheckConstraint(
            "provider_number_sid IS NULL OR provider_number_sid ~ '^PN[0-9a-fA-F]{32}$'",
            name="provider_number_sid",
        ),
    )
    number: Mapped[str] = mapped_column(String(16), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    # Set when ownership was proven against the tenant's own provider connection.
    integration_id: Mapped[UUID | None]
    provider_number_sid: Mapped[str | None] = mapped_column(String(34))


class Call(TenantRecord):
    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_calls_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            name="fk_calls_scope_location_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "access_patient_id"],
            ["patients.tenant_id", "patients.id"],
            name="fk_calls_access_patient_id_patients",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "verified_patient_id"],
            ["patients.tenant_id", "patients.id"],
            name="fk_calls_verified_patient_id_patients",
        ),
        CheckConstraint("access_attempts BETWEEN 0 AND 3", name="access_attempts_bounded"),
        ForeignKeyConstraint(
            ["tenant_id", "phone_number_id"], ["phone_numbers.tenant_id", "phone_numbers.id"]
        ),
        CheckConstraint(
            "status IN ('received', 'active', 'completed', 'disconnected', 'failed')",
            name="status_allowed",
        ),
        CheckConstraint("received_frames >= 0 AND sent_frames >= 0", name="frame_counts"),
        ForeignKeyConstraint(
            ["tenant_id", "integration_id"],
            ["tenant_integrations.tenant_id", "tenant_integrations.id"],
            name="fk_calls_integration",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "credential_version_id"],
            ["integration_credential_versions.tenant_id", "integration_credential_versions.id"],
            name="fk_calls_credential_version",
        ),
        CheckConstraint(
            "(integration_id IS NULL) = (credential_version_id IS NULL)",
            name="integration_binding",
        ),
    )
    # Provider binding pinned when the call was accepted; later events must match it.
    integration_id: Mapped[UUID | None]
    credential_version_id: Mapped[UUID | None]
    location_id: Mapped[UUID | None]
    access_patient_id: Mapped[UUID | None]
    verified_patient_id: Mapped[UUID | None]
    access_digest: Mapped[str | None] = mapped_column(String(128))
    access_salt: Mapped[str | None] = mapped_column(String(32))
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    phone_number_id: Mapped[UUID]
    provider_call_id: Mapped[str] = mapped_column(String(34), unique=True)
    stream_sid: Mapped[str | None] = mapped_column(String(34))
    status: Mapped[str] = mapped_column(String(20), default="received")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_frames: Mapped[int] = mapped_column(default=0)
    sent_frames: Mapped[int] = mapped_column(default=0)
    end_reason: Mapped[str | None] = mapped_column(String(40))
