from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from app.events.providers import (
    PermanentProviderError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitedProviderError,
    TransientProviderError,
)


class TTSError(ProviderError):
    """Base exception for Text-to-Speech failures."""

    code = "tts_error"
    retryable = False


class TTSTransientError(TTSError, TransientProviderError):
    code = "tts_unavailable"
    retryable = True


class TTSTimeoutError(TTSTransientError, ProviderTimeoutError):
    code = "tts_timeout"


class TTSRateLimitError(TTSTransientError, RateLimitedProviderError):
    code = "tts_rate_limited"


class TTSPermanentError(TTSError, PermanentProviderError):
    code = "tts_rejected"


@dataclass(frozen=True)
class TTSChunk:
    audio: bytes
    sample_rate: int = 8000
    is_final: bool = False


class TextToSpeechProvider(Protocol):
    """Protocol for Text-to-Speech synthesis engines."""

    @property
    def name(self) -> str: ...

    def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        """Stream raw audio chunks (PCM16 8000Hz mono)."""
        ...

    async def health(self) -> bool: ...
