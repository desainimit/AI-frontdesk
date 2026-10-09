from datetime import date, time
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Integer,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class BusinessService(TenantRecord):
    __tablename__ = "business_services"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_business_services_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_business_services_name"),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            name="fk_business_services_location",
        ),
        CheckConstraint(
            "revision >= 1 AND price_minor BETWEEN 0 AND 1000000000 "
            "AND capacity BETWEEN 1 AND 1000 AND minimum_notice_minutes BETWEEN 0 AND 43200",
            name="positive_values",
        ),
        CheckConstraint(
            "duration_minutes BETWEEN 5 AND 1440 "
            "AND buffer_before_minutes BETWEEN 0 AND 240 "
            "AND buffer_after_minutes BETWEEN 0 AND 240",
            name="duration_values",
        ),
        CheckConstraint("currency = 'INR'", name="currency"),
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(4000))
    price_minor: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    duration_minutes: Mapped[int]
    buffer_before_minutes: Mapped[int]
    buffer_after_minutes: Mapped[int]
    minimum_notice_minutes: Mapped[int]
    capacity: Mapped[int]
    location_id: Mapped[UUID | None]
    is_active: Mapped[bool]
    revision: Mapped[int] = mapped_column(Integer, default=1)


class BusinessStaff(TenantRecord):
    __tablename__ = "business_staff"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_business_staff_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_business_staff_name"),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            name="fk_business_staff_location",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"], ["users.tenant_id", "users.id"], name="fk_business_staff_user"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "calendar_connection_id"],
            ["tenant_integrations.tenant_id", "tenant_integrations.id"],
            name="fk_business_staff_calendar",
        ),
        CheckConstraint("revision >= 1 AND capacity BETWEEN 1 AND 100", name="positive_values"),
    )
    name: Mapped[str] = mapped_column(String(200))
    job_title: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(16))
    transfer_number: Mapped[str | None] = mapped_column(String(16))
    user_id: Mapped[UUID | None]
    location_id: Mapped[UUID | None]
    calendar_connection_id: Mapped[UUID | None]
    capacity: Mapped[int]
    is_active: Mapped[bool]
    revision: Mapped[int] = mapped_column(Integer, default=1)


class BusinessResource(TenantRecord):
    __tablename__ = "business_resources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_business_resources_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_business_resources_name"),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            name="fk_business_resources_location",
        ),
        CheckConstraint("revision >= 1 AND capacity BETWEEN 1 AND 1000", name="positive_values"),
        CheckConstraint("resource_type IN ('space','equipment')", name="resource_type"),
    )
    name: Mapped[str] = mapped_column(String(200))
    resource_type: Mapped[str] = mapped_column(String(20))
    description: Mapped[str] = mapped_column(String(2000))
    location_id: Mapped[UUID]
    capacity: Mapped[int]
    is_active: Mapped[bool]
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ServiceStaff(TenantRecord):
    __tablename__ = "business_service_staff"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["business_services.tenant_id", "business_services.id"],
            name="fk_business_service_staff_service",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"],
            ["business_staff.tenant_id", "business_staff.id"],
            name="fk_business_service_staff_staff",
        ),
        UniqueConstraint(
            "tenant_id", "service_id", "staff_id", name="uq_business_service_staff_binding"
        ),
    )
    service_id: Mapped[UUID]
    staff_id: Mapped[UUID]


class ServiceResource(TenantRecord):
    __tablename__ = "business_service_resources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "service_id"],
            ["business_services.tenant_id", "business_services.id"],
            name="fk_business_service_resources_service",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "resource_id"],
            ["business_resources.tenant_id", "business_resources.id"],
            name="fk_business_service_resources_resource",
        ),
        UniqueConstraint(
            "tenant_id", "service_id", "resource_id", name="uq_business_service_resources_binding"
        ),
    )
    service_id: Mapped[UUID]
    resource_id: Mapped[UUID]


class StaffWindow(TenantRecord):
    __tablename__ = "business_staff_windows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"],
            ["business_staff.tenant_id", "business_staff.id"],
            name="fk_business_staff_windows_staff",
        ),
        CheckConstraint("weekday BETWEEN 0 AND 6 AND opens < closes", name="valid_window"),
    )
    staff_id: Mapped[UUID]
    weekday: Mapped[int]
    opens: Mapped[time] = mapped_column(Time(timezone=False))
    closes: Mapped[time] = mapped_column(Time(timezone=False))


class StaffHoliday(TenantRecord):
    __tablename__ = "business_staff_holidays"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "staff_id"],
            ["business_staff.tenant_id", "business_staff.id"],
            name="fk_business_staff_holidays_staff",
        ),
        UniqueConstraint("tenant_id", "staff_id", "day", name="uq_business_staff_holidays_day"),
    )
    staff_id: Mapped[UUID]
    day: Mapped[date] = mapped_column(Date)


class DirectoryMutation(TenantRecord):
    __tablename__ = "business_directory_mutations"
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
