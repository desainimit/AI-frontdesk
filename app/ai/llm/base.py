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
    # The model that wrote provider_state. Another model is never shown it: the backup
    # can take over between the steps of one reply.
    state_model: str = ""


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: dict[str, Any]


Turn = UserTurn | AssistantTurn | ToolResult


@dataclass(frozen=True)
class ChatRequest:
    # The instructions that stay the same through a conversation, so a provider can
    # keep them cached between turns.
    system: str
    turns: tuple[Turn, ...]
    tools: tuple[ToolSpec, ...] = ()
    # What changes every turn (the time, where the conversation stands); sent after
    # the fixed part.
    context: str = ""
    # The conversation, so a provider sends its turns to the endpoint holding its cache.
    session_id: str = ""

    @property
    def instructions(self) -> str:
        return f"{self.system}\n\n{self.context}" if self.context else self.system


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Of input_tokens, those read from the provider's cache at a lower price.
    cached_tokens: int = 0
    # US dollars: the provider's own figure when it reports one, else the catalog price.
    cost_usd: float = 0.0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_tokens + other.cached_tokens,
            self.cost_usd + other.cost_usd,
        )


class UsageMeter:
    """What one conversation's quick checks (safety, yes/no, summary) used, taken with
    the reply they ran beside."""

    def __init__(self) -> None:
        self._usage = Usage()

    def add(self, usage: Usage) -> None:
        self._usage = self._usage + usage

    def take(self) -> Usage:
        usage, self._usage = self._usage, Usage()
        return usage


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
    # Where the request was served (OpenRouter's upstream, such as "Google Vertex").
    served_by: str = ""


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
