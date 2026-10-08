from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.auth.context import Role


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    role: Role
    role_id: UUID | None = None
    role_name: str = ""
    is_active: bool
    created_at: datetime
    # "invited" has not accepted this membership; existing identities keep their password.
    status: Literal["active", "invited", "deactivated"] = "active"
