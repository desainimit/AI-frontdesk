from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class Appointment(TenantRecord):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_appointments_tenant_id_id"),
        ForeignKeyConstraint(["tenant_id", "provider_id"], ["providers.tenant_id", "providers.id"]),
        ForeignKeyConstraint(["tenant_id", "patient_id"], ["patients.tenant_id", "patients.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["services.tenant_id", "services.id"],
            name="fk_appointments_service",
        ),
        CheckConstraint("ends_at > starts_at", name="positive_duration"),
        CheckConstraint("status IN ('booked', 'cancelled')", name="status_allowed"),
        CheckConstraint("version >= 1", name="positive_version"),
        ExcludeConstraint(
            ("tenant_id", "="),
            ("provider_id", "="),
            (text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            where=text("status = 'booked'"),
            name="ex_appointments_provider_overlap",
            using="gist",
        ),
        ExcludeConstraint(
            ("tenant_id", "="),
            ("patient_id", "="),
            (text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            where=text("status = 'booked'"),
            name="ex_appointments_patient_overlap",
            using="gist",
        ),
    )
    provider_id: Mapped[UUID]
    patient_id: Mapped[UUID]
    # The appointment type; empty for a booking made without one.
    service_id: Mapped[UUID | None]
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="booked")
    version: Mapped[int] = mapped_column(Integer, default=1)
