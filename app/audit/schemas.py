from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AuditView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    actor_id: UUID | None
    platform_user_id: UUID | None = None
    support_session_id: UUID | None = None
    actor_type: str
    action: str
    resource_type: str
    resource_id: UUID | None
    request_id: UUID
    call_id: UUID | None
    created_at: datetime
