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


class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
    async def generate_structured[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> T: ...
    async def close(self) -> None: ...
