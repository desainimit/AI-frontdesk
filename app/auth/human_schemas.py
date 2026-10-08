from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class EmailIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    tenant_id: UUID
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

    @field_validator("email")
    @classmethod
    def normalize(cls, value: str) -> str:
        return value.lower()


class HumanLogin(EmailIdentity):
    password: SecretStr = Field(min_length=1, max_length=128)


class NewPassword(BaseModel):
    password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator("password")
    @classmethod
    def password_policy(cls, value: SecretStr) -> SecretStr:
        text = value.get_secret_value().casefold()
        if len(set(text)) < 5 or text in {
            "password123456789",
            "123456789012345",
            "qwertyuiopasdfgh",
            "thisisapassword123",
        }:
            raise ValueError("Choose a less predictable password")
        return value


class InvitationToken(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=1, max_length=160)


class AcceptInvitation(InvitationToken):
    password: SecretStr | None = Field(default=None, min_length=15, max_length=128)

    @field_validator("password")
    @classmethod
    def password_policy(cls, value: SecretStr | None) -> SecretStr | None:
        return NewPassword.password_policy(value) if value is not None else None


class InvitationDetails(BaseModel):
    tenant_id: UUID
    business_name: str
    display_name: str
    role_name: str
    expires_at: datetime
    password_required: bool
    account_requires_recovery: bool


class ResetPassword(EmailIdentity, NewPassword):
    otp: SecretStr = Field(min_length=8, max_length=8)


class HumanSessionView(BaseModel):
    access_token: str = Field(repr=False)
    tenant_id: UUID
    expires_at: datetime


class InvitationView(BaseModel):
    id: UUID
    tenant_id: UUID
    user_id: UUID
    expires_at: datetime
    delivery_status: str
    consumed: bool


class InviteMember(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="member", pattern="^(admin|member)$")
    role_id: UUID | None = None

    @field_validator("email")
    @classmethod
    def normalize(cls, value: str) -> str:
        return value.lower()


class TeamMemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: str = Field(pattern="^(admin|member)$")
    is_active: bool
    role_id: UUID | None = None
