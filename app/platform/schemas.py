from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class LoginCommand(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        hide_input_in_errors=True,
        json_schema_extra={
            "example": {
                "email": "admin@example.com",
                "password": "your-secure-password",
            }
        },
    )
    email: str = Field(
        min_length=3,
        max_length=254,
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
        examples=["admin@example.com"],
    )
    password: SecretStr = Field(
        min_length=1,
        max_length=256,
        examples=["your-secure-password"],
    )
    otp: SecretStr | None = Field(default=None, min_length=6, max_length=6)
    recovery_code: SecretStr | None = Field(default=None, min_length=32, max_length=64)

    @model_validator(mode="after")
    def one_factor_proof(self) -> "LoginCommand":
        if self.otp is not None and self.recovery_code is not None:
            raise ValueError("Use an authenticator code or a recovery code")
        return self

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


class SuperAdminSeed(LoginCommand):
    password: SecretStr = Field(min_length=8, max_length=256)
    display_name: str = Field(default="Super Admin", min_length=1, max_length=200)


class PlatformUserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    display_name: str
    role: Literal["super_admin"]
    mfa_verified: bool = False


class SessionView(BaseModel):
    # Token is deliberately returned only once in this response, never in audit records.
    access_token: str = Field(repr=False)
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    mfa_verified: bool = False
    mfa_recovered: bool = False
