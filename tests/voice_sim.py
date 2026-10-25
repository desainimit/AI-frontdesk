"""A stand-in phone line for timing tests: speech that takes as long as a real service
to start, and a Twilio side that notes when each frame was sent."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from app.voice.stt.base import STTStream, TranscriptionResult


class TimedTTS:
    """µ-law speech that starts after `first_byte` seconds, like a real service."""

    name = "timed"

    def __init__(self, *, first_byte: float = 0.2, seconds_per_char: float = 0.02) -> None:
        self.first_byte = first_byte
        self.seconds_per_char = seconds_per_char
        self.requests: list[str] = []

    async def synthesize_ulaw(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        self.requests.append(text)
        await asyncio.sleep(self.first_byte)
        frames = max(1, int(len(text) * self.seconds_per_char / 0.02))
        for _ in range(frames):
            yield b"\xff" * 160

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        raise AssertionError("µ-law is used directly")
        yield b""  # pragma: no cover

    async def health(self) -> bool:
        return True


@dataclass
class Line:
    """What the pipeline sent to Twilio, with the time of each message."""

    sent: list[tuple[float, dict[str, Any]]] = field(
        default_factory=list[tuple[float, dict[str, Any]]]
    )

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append((monotonic(), message))

    def media_times(self) -> list[float]:
        return [at for at, message in self.sent if message.get("event") == "media"]

    def played_gaps(self) -> list[float]:
        """Silences the caller would hear between frames.

        Twilio plays frames back to back from its buffer, so a silence only happens when
        a frame arrives after the previous one has finished playing.
        """
        gaps: list[float] = []
        playing_until = 0.0
        for at in self.media_times():
            if playing_until and at > playing_until:
                gaps.append(at - playing_until)
            playing_until = max(playing_until, at) + 0.02
        return gaps


class SilentSTT:
    name = "silent"

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        return _SilentStream()

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        return TranscriptionResult("", True)

    async def health(self) -> bool:
        return True


class _SilentStream:
    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def send_audio(self, audio: bytes) -> None:
        return None

    async def close(self) -> None:
        self._closed.set()

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        await self._closed.wait()
        return
        yield  # pragma: no cover
