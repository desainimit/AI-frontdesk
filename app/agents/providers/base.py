from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["system", "developer", "user"]
    content: str


@dataclass(frozen=True)
class LLMRequest:
    messages: tuple[LLMMessage, ...]


@dataclass(frozen=True)
class LLMResponse:
    text: str


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
    """Empty, truncated or schema-violating output."""

    code = "llm_invalid_response"


class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> T: ...
    async def close(self) -> None: ...
