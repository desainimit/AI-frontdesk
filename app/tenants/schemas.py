from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TenantView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    is_active: bool
    created_at: datetime
    service_state: str
