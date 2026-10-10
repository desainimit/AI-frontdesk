from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateProvider(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "location_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "display_name": "Dr. Sarah Connor",
                "specialty": "General Medicine",
            }
        },
    )
    location_id: UUID
    display_name: str = Field(min_length=1, max_length=200, examples=["Dr. Sarah Connor"])
    specialty: str | None = Field(
        default=None, min_length=1, max_length=200, examples=["General Medicine"]
    )


class UpdateProvider(CreateProvider):
    is_active: bool = True


class ProviderView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    location_id: UUID
    display_name: str
    specialty: str | None
    is_active: bool
    created_at: datetime
