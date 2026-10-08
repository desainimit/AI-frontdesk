from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreatePatient(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "display_name": "Jane Doe",
                "phone": "+919876543210",
            }
        },
    )
    display_name: str = Field(min_length=1, max_length=200, examples=["Jane Doe"])
    phone: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$", max_length=16, examples=["+919876543210"])
    date_of_birth: date | None = None
    account_identifier: str | None = Field(
        default=None, min_length=4, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )


class PatientView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    display_name: str
    phone: str
    created_at: datetime


class UpdatePatientVerification(BaseModel):
    """Administrative factors are write-only; neither appears in patient views."""

    model_config = ConfigDict(extra="forbid", strict=True)
    date_of_birth: date | None = None
    account_identifier: str | None = Field(
        default=None, min_length=4, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )
