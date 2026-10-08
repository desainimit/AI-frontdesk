from datetime import UTC, datetime, timedelta
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class TimeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    starts_at: AwareDatetime

    @field_validator("starts_at")
    @classmethod
    def utc_start(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class TimeWindow(TimeCommand):
    ends_at: AwareDatetime

    @field_validator("ends_at")
    @classmethod
    def utc_end(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class DefineAvailability(TimeWindow):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "starts_at": "2026-10-01T09:00:00+05:30",
                "ends_at": "2026-10-01T17:00:00+05:30",
                "slot_minutes": 30,
            }
        },
    )
    slot_minutes: int = Field(ge=5, le=240, examples=[30])

    @model_validator(mode="after")
    def bounded(self) -> Self:
        duration = self.ends_at - self.starts_at
        if duration > timedelta(days=1) or duration < timedelta(minutes=self.slot_minutes):
            raise ValueError("Window must fit a slot and last at most 24 hours")
        return self


class SlotQuery(TimeWindow):
    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.ends_at - self.starts_at > timedelta(days=31):
            raise ValueError("Query at most 31 days of availability")
        return self


class BookAppointment(TimeCommand):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "starts_at": "2026-10-01T09:00:00+05:30",
                "provider_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "patient_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
            }
        },
    )
    provider_id: UUID
    patient_id: UUID


class RescheduleAppointment(TimeCommand):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "starts_at": "2026-10-01T10:00:00+05:30",
                "expected_version": 1,
            }
        },
    )
    expected_version: int = Field(ge=1, examples=[1])


class CancelAppointment(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "expected_version": 1,
            }
        },
    )
    expected_version: int = Field(ge=1, examples=[1])
