from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CreateLocation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "name": "Main Clinic",
                "timezone": "Asia/Kolkata",
            }
        },
    )
    name: str = Field(min_length=1, max_length=200, examples=["Main Clinic"])
    timezone: str = Field(
        default="Asia/Kolkata", min_length=1, max_length=100, examples=["Asia/Kolkata"]
    )

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Use an IANA timezone, such as Asia/Kolkata") from None
        return value


class LocationView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    timezone: str
    created_at: datetime
