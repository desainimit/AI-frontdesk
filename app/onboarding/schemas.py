from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.auth.policies import Permission

SetupStep = Literal[
    "business",
    "administrator",
    "team",
    "agent",
    "knowledge",
    "channels",
    "billing",
    "tests",
    "publication",
]


class SetupCheck(BaseModel):
    key: str
    ready: bool
    message: str


class SetupLink(BaseModel):
    label: str
    path: str
    permission: Permission


class SetupStepView(BaseModel):
    key: SetupStep
    title: str
    status: Literal["restricted", "blocked", "needs_review", "reviewed"]
    fingerprint: str | None
    checks: list[SetupCheck]
    links: list[SetupLink]
    review_permissions: list[Permission]


class SetupView(BaseModel):
    tenant_id: UUID
    revision: int
    configuration_revision: int
    published_version: int | None
    current_step: SetupStep
    service_state: Literal["setup", "live"]
    steps: list[SetupStepView]


class SetupCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    configuration_revision: int = Field(ge=1, strict=True)
    step: SetupStep
    action: Literal["navigate", "review"]
    expected_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
