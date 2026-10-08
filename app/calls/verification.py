"""Tenant-owned requirements for reception-assisted call verification."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VerificationFactor = Literal["reception_attestation", "date_of_birth", "account_identifier"]


class VerificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    required_factors: list[VerificationFactor] = Field(
        default_factory=lambda: ["reception_attestation"]
    )
    code_ttl_seconds: int = Field(default=300, ge=60, le=600)
    access_ttl_minutes: int = Field(default=120, ge=5, le=240)
    max_code_attempts: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def require_human_attestation(self) -> "VerificationPolicy":
        if "reception_attestation" not in self.required_factors:
            raise ValueError("Reception attestation is required for patient access")
        if len(self.required_factors) != len(set(self.required_factors)):
            raise ValueError("Verification factors must be unique")
        return self
