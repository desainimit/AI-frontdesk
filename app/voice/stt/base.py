import math
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


class STTError(ProviderError):
    """Base exception for Speech-to-Text failures."""

    code = "stt_error"
    retryable = False


class STTTransientError(STTError, TransientProviderError):
    code = "stt_unavailable"
    retryable = True


class STTTimeoutError(STTTransientError, ProviderTimeoutError):
    code = "stt_timeout"


class STTRateLimitError(STTTransientError, RateLimitedProviderError):
    code = "stt_rate_limited"


class STTPermanentError(STTError, PermanentProviderError):
    code = "stt_rejected"


def _validate_text(value: object) -> None:
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError("Invalid transcript text")


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    is_final: bool
    confidence: float = 1.0
    language: str | None = None
    duration_ms: float = 0.0
    languages: tuple[str, ...] = ()
    # The recognizer heard the caller begin to speak; the text may still be empty.
    speech_started: bool = False

    def __post_init__(self) -> None:
        _validate_text(self.text)
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("Invalid transcript confidence")


class STTStream(Protocol):
    """Streaming connection to an STT provider."""

    async def send_audio(self, audio: bytes) -> None:
        """Push raw PCM16 or µ-law audio chunk into the active recognition stream."""
        ...

    async def close(self) -> None:
        """Gracefully close the audio input stream."""
        ...

    def results(self) -> AsyncIterator[TranscriptionResult]:
        """Yield real-time interim and final transcription results."""
        ...


class SpeechToTextProvider(Protocol):
    """Protocol for Speech-to-Text engines; hides vendor SDKs and network protocols."""

    @property
    def name(self) -> str: ...

    async def create_stream(
        self, sample_rate: int = 8000, encoding: str = "linear16"
    ) -> STTStream: ...

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult: ...

    async def health(self) -> bool: ...
