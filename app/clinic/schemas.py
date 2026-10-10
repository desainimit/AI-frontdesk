from datetime import date, time
from itertools import pairwise
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ServiceCommand(_Command):
    name: str = Field(min_length=1, max_length=200, examples=["New patient visit"])
    description: str = Field(default="", max_length=1000)
    duration_minutes: int = Field(ge=5, le=240, multiple_of=5, examples=[30])
    is_active: bool = True


class ServiceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    description: str
    duration_minutes: int
    is_active: bool


class ScheduleWindow(_Command):
    weekday: int = Field(ge=0, le=6)  # 0 is Monday
    starts: time
    ends: time

    @model_validator(mode="after")
    def ordered(self) -> "ScheduleWindow":
        if self.starts.tzinfo is not None or self.ends.tzinfo is not None:
            raise ValueError("Working hours are local times without a UTC offset")
        if self.starts >= self.ends:
            raise ValueError("The end time must be after the start time")
        return self


class WeeklySchedule(_Command):
    windows: list[ScheduleWindow] = Field(max_length=42)

    @model_validator(mode="after")
    def nonoverlapping(self) -> "WeeklySchedule":
        for day in range(7):
            windows = sorted((w for w in self.windows if w.weekday == day), key=lambda w: w.starts)
            if any(a.ends > b.starts for a, b in pairwise(windows)):
                raise ValueError("Working hours on the same day cannot overlap")
        return self


class TimeOffCommand(_Command):
    starts_on: date
    ends_on: date
    reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def ordered(self) -> "TimeOffCommand":
        if self.ends_on < self.starts_on:
            raise ValueError("The last day cannot be before the first day")
        if (self.ends_on - self.starts_on).days > 366:
            raise ValueError("Time off can cover at most one year")
        return self


class TimeOffView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    starts_on: date
    ends_on: date
    reason: str


class ProviderScheduleView(BaseModel):
    provider_id: UUID
    # The clock these times are in.
    timezone: str
    windows: list[ScheduleWindow]
    time_off: list[TimeOffView]
