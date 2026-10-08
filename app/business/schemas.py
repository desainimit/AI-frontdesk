from datetime import date, time
from itertools import pairwise
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BusinessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    is_active: bool = True


class StaffAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday: int = Field(ge=0, le=6)
    opens: time
    closes: time

    @model_validator(mode="after")
    def valid_window(self) -> "StaffAvailability":
        if (
            self.opens.tzinfo is not None
            or self.closes.tzinfo is not None
            or self.opens >= self.closes
        ):
            raise ValueError("Use ordered local availability times without UTC offsets")
        return self


class ServiceDefinition(BusinessRecord):
    kind: Literal["service"] = "service"
    description: str = Field(default="", max_length=4000)
    price_minor: int = Field(default=0, ge=0, le=1000000000)
    currency: Literal["INR"] = "INR"
    duration_minutes: int = Field(default=30, ge=5, le=1440)
    buffer_before_minutes: int = Field(default=0, ge=0, le=240)
    buffer_after_minutes: int = Field(default=0, ge=0, le=240)
    minimum_notice_minutes: int = Field(default=60, ge=0, le=43200)
    capacity: int = Field(default=1, ge=1, le=1000)
    location_id: UUID | None = None
    staff_ids: list[UUID] = Field(default_factory=list[UUID], max_length=200)
    resource_ids: list[UUID] = Field(default_factory=list[UUID], max_length=200)

    @model_validator(mode="after")
    def distinct_assignments(self) -> "ServiceDefinition":
        if len(set(self.staff_ids)) != len(self.staff_ids) or len(set(self.resource_ids)) != len(
            self.resource_ids
        ):
            raise ValueError("Assignments must be unique")
        return self


class StaffDefinition(BusinessRecord):
    kind: Literal["staff"] = "staff"
    job_title: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=254, pattern=r"^(|[^\s@]+@[^\s@]+\.[^\s@]+)$")
    phone: str = Field(default="", pattern=r"^(|\+[1-9][0-9]{7,14})$")
    transfer_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")
    user_id: UUID | None = None
    location_id: UUID | None = None
    calendar_connection_id: UUID | None = None
    capacity: int = Field(default=1, ge=1, le=100)
    availability: list[StaffAvailability] = Field(
        default_factory=list[StaffAvailability], max_length=42
    )
    holidays: list[date] = Field(default_factory=list[date], max_length=366)

    @model_validator(mode="after")
    def valid_schedule(self) -> "StaffDefinition":
        for day in range(7):
            windows = sorted(
                (w for w in self.availability if w.weekday == day), key=lambda w: w.opens
            )
            if any(before.closes > after.opens for before, after in pairwise(windows)):
                raise ValueError("Availability windows cannot overlap")
        if len(set(self.holidays)) != len(self.holidays):
            raise ValueError("Staff holidays must be unique")
        return self


class ResourceDefinition(BusinessRecord):
    kind: Literal["resource"] = "resource"
    resource_type: Literal["space", "equipment"] = "space"
    location_id: UUID
    capacity: int = Field(default=1, ge=1, le=1000)
    description: str = Field(default="", max_length=2000)


class DirectorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    services: list[ServiceDefinition] = Field(
        default_factory=list[ServiceDefinition], max_length=500
    )
    staff: list[StaffDefinition] = Field(default_factory=list[StaffDefinition], max_length=200)
    resources: list[ResourceDefinition] = Field(
        default_factory=list[ResourceDefinition], max_length=200
    )

    @model_validator(mode="after")
    def unique_records(self) -> "DirectorySnapshot":
        records = [*self.services, *self.staff, *self.resources]
        if len({record.id for record in records}) != len(records):
            raise ValueError("Directory identities must be unique")
        for group in (self.services, self.staff, self.resources):
            if len({record.name.casefold() for record in group}) != len(group):
                raise ValueError("Directory names must be unique within each kind")
        return self


class DirectoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    draft_revision: int = Field(ge=1, strict=True)
    expected_revision: int = Field(ge=0, strict=True)
    record: ServiceDefinition | StaffDefinition | ResourceDefinition = Field(discriminator="kind")


class DirectoryWriteResult(BaseModel):
    request_id: UUID
    draft_revision: int
    entity_revision: int
    record: ServiceDefinition | StaffDefinition | ResourceDefinition = Field(discriminator="kind")


class DirectoryView(BaseModel):
    draft_revision: int
    published_version: int | None
    records: DirectorySnapshot
    revisions: dict[UUID, int]


class BusinessListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["service", "staff", "resource"] = "service"
    search: str = Field(default="", max_length=200)
    active: bool | None = None
    location_id: UUID | None = None
    limit: int = Field(default=25, ge=25, le=100)
    cursor: str | None = Field(default=None, max_length=4096)

    @field_validator("limit")
    @classmethod
    def supported_page_size(cls, value: int) -> int:
        if value not in (25, 50, 100):
            raise ValueError("Use a supported page size")
        return value


class BusinessListItem(BaseModel):
    revision: int
    record: ServiceDefinition | StaffDefinition | ResourceDefinition = Field(discriminator="kind")


class BusinessListPage(BaseModel):
    draft_revision: int
    published_version: int | None
    items: list[BusinessListItem]
    next_cursor: str | None


class BusinessRecordView(BaseModel):
    draft_revision: int
    item: BusinessListItem
