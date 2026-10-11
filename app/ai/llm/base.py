"""What the assistant needs from a model provider, whichever provider it is."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["system", "developer", "user"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    """One self-contained question with a structured answer (a check or a summary)."""

    messages: tuple[LLMMessage, ...]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # JSON Schema of the arguments, already reduced to what providers accept.
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    # None when the model produced arguments that are not a JSON object.
    arguments: dict[str, Any] | None


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    # Whatever the provider must be shown again, unchanged, to continue after a tool
    # call (Gemini's thought signatures). Opaque to everything but its own adapter.
    provider_state: Any = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: dict[str, Any]


Turn = UserTurn | AssistantTurn | ToolResult


@dataclass(frozen=True)
class ChatRequest:
    system: str
    turns: tuple[Turn, ...]
    tools: tuple[ToolSpec, ...] = ()


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    """Always the last event of a stream."""

    turn: AssistantTurn
    # "length": the reply was cut off at the output limit.
    finish: Literal["stop", "tool_calls", "length"]
    usage: Usage = field(default_factory=Usage)
    model: str = ""


ModelEvent = TextDelta | Completed


class LLMProviderError(RuntimeError):
    """Static provider error; credentials, prompts and response bodies stay private."""

    code = "llm_error"
    retryable = False


class LLMRateLimitError(LLMProviderError):
    code = "llm_rate_limited"
    retryable = True


class LLMTimeoutError(LLMProviderError):
    code = "llm_timeout"
    retryable = True


class LLMUnavailableError(LLMProviderError):
    code = "llm_unavailable"
    retryable = True


class LLMCredentialsError(LLMProviderError):
    """The key was refused or lacks access; retrying cannot help."""

    code = "llm_credentials_rejected"


class LLMCreditsError(LLMProviderError):
    """The provider account has run out of credit; only topping up helps."""

    code = "llm_credits_exhausted"


class LLMRejectedError(LLMProviderError):
    """The provider refused the request itself, such as an unknown model."""

    code = "llm_request_rejected"


class LLMBlockedError(LLMProviderError):
    code = "llm_response_blocked"


class LLMInvalidResponseError(LLMProviderError):
    """Empty or malformed output."""

    code = "llm_invalid_response"


class LLMInterruptedError(LLMProviderError):
    """The stream broke after part of the reply was delivered; it is never replayed."""

    code = "llm_interrupted"


class LLMProvider(Protocol):
    def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        """Text as it is produced, then one Completed. Raises before or between events."""
        ...

    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> T: ...
    async def close(self) -> None: ...
