from datetime import date, time
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Integer,
    String,
    Time,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class Service(TenantRecord):
    """An appointment type, such as "New patient visit". Its length sets the slot size."""

    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_services_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_services_name"),
        CheckConstraint("duration_minutes BETWEEN 5 AND 240", name="duration_range"),
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    duration_minutes: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class ProviderSchedule(TenantRecord):
    """One working window in a provider's week, in the clock time of their location."""

    __tablename__ = "provider_schedules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["providers.tenant_id", "providers.id"],
            name="fk_provider_schedules_provider",
        ),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday"),
        CheckConstraint("starts < ends", name="ordered_window"),
    )
    provider_id: Mapped[UUID]
    weekday: Mapped[int]  # 0 is Monday
    starts: Mapped[time] = mapped_column(Time(timezone=False))
    ends: Mapped[time] = mapped_column(Time(timezone=False))


class ProviderTimeOff(TenantRecord):
    """Whole days a provider is away. Both ends are included."""

    __tablename__ = "provider_time_off"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["providers.tenant_id", "providers.id"],
            name="fk_provider_time_off_provider",
        ),
        CheckConstraint("ends_on >= starts_on", name="ordered_days"),
    )
    provider_id: Mapped[UUID]
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(200), default="", server_default="")
