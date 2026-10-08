from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.auth.human_schemas import NewPassword


class GlobalEmail(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

    @field_validator("email")
    @classmethod
    def normalized(cls, value: str) -> str:
        return value.lower()


class IdentityLogin(GlobalEmail):
    password: SecretStr = Field(min_length=1, max_length=128)


class GlobalRecoveryView(BaseModel):
    recovery_token: str = Field(repr=False)
    expires_at: datetime


class GlobalPasswordReset(GlobalEmail, NewPassword):
    recovery_token: SecretStr = Field(min_length=100, max_length=2048)
    otp: SecretStr = Field(min_length=8, max_length=8)


class IdentityCredential(GlobalEmail):
    token: SecretStr = Field(min_length=80, max_length=90)


class WorkspaceSelection(IdentityCredential):
    tenant_id: UUID


class IdentitySessionView(BaseModel):
    access_token: str = Field(repr=False)
    email: str
    expires_at: datetime


class WorkspaceOption(BaseModel):
    tenant_id: UUID
    business_name: str
    user_id: UUID
    display_name: str
    role_name: str
    service_state: str


class WorkspacesView(BaseModel):
    items: list[WorkspaceOption]
    expires_at: datetime
