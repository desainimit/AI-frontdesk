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


class Availability(TenantRecord):
    __tablename__ = "availability"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "provider_id"], ["providers.tenant_id", "providers.id"]),
        CheckConstraint("ends_at > starts_at", name="positive_window"),
        CheckConstraint("ends_at <= starts_at + interval '24 hours'", name="bounded_window"),
        CheckConstraint("slot_minutes BETWEEN 5 AND 240", name="slot_minutes_range"),
        CheckConstraint(
            "EXTRACT(EPOCH FROM (ends_at - starts_at)) >= slot_minutes * 60", name="fits_slot"
        ),
        ExcludeConstraint(
            ("tenant_id", "="),
            ("provider_id", "="),
            (text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            name="ex_availability_provider_overlap",
            using="gist",
        ),
    )
    provider_id: Mapped[UUID]
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    slot_minutes: Mapped[int] = mapped_column(Integer)


class Appointment(TenantRecord):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_appointments_tenant_id_id"),
        ForeignKeyConstraint(["tenant_id", "provider_id"], ["providers.tenant_id", "providers.id"]),
        ForeignKeyConstraint(["tenant_id", "patient_id"], ["patients.tenant_id", "patients.id"]),
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
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="booked")
    version: Mapped[int] = mapped_column(Integer, default=1)
