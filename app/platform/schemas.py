from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class SuperAdminSeed(BaseModel):
    """The first platform administrator, supplied by the operator to the setup job."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: SecretStr = Field(min_length=8, max_length=256)
    display_name: str = Field(default="Super Admin", min_length=1, max_length=200)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value
