from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Intent(StrEnum):
    GREETING = "greeting"
    CLINIC_INQUIRY = "clinic_inquiry"
    PROVIDER_INQUIRY = "provider_inquiry"
    BOOK_APPOINTMENT = "book_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    HUMAN_TRANSFER = "human_transfer"
    EMERGENCY = "emergency"
    UNKNOWN = "unknown"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    arguments_json: str = Field(min_length=2, max_length=4096)


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    response: str = Field(max_length=1000)
    tool_calls: list[ToolCall] = Field(max_length=3)

    @model_validator(mode="after")
    def coherent_decision(self) -> Self:
        if self.tool_calls and self.response.strip():
            raise ValueError("Tool requests cannot include a spoken response")
        if not self.tool_calls and not self.response.strip():
            raise ValueError("A final decision requires a response")
        return self


class AgentResult(BaseModel):
    intent: Intent
    response: str
    tools_used: list[str] = Field(default_factory=list)
    status: Literal["completed", "unavailable", "blocked"] = "completed"
