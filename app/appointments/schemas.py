from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Slot(BaseModel):
    starts_at: datetime
    ends_at: datetime


class AppointmentView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    provider_id: UUID
    patient_id: UUID
    service_id: UUID | None = None
    starts_at: datetime
    ends_at: datetime
    status: Literal["booked", "cancelled"]
    version: int
    created_at: datetime
    updated_at: datetime
    # Display labels for lists. The patient name is present only for callers who may
    # read patients; an appointment reader without that permission gets None.
    provider_name: str | None = None
    patient_name: str | None = None
    service_name: str | None = None
