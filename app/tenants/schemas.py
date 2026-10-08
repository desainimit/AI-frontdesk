from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.calls.verification import VerificationPolicy
from app.conversation.locales import LanguagePolicy


class TenantView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    is_active: bool
    created_at: datetime
    language_policy: LanguagePolicy
    human_transfer_number: str | None
    service_state: str
    verification_policy: VerificationPolicy


class TenantName(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)


class TransferDestination(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")
