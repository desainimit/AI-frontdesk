import asyncio
from collections.abc import AsyncIterator

from app.voice.tts.base import TTSError


class MockTTSProvider:
    """Deterministic mock TTS provider for testing streaming and fallbacks."""

    def __init__(
        self,
        name: str = "mock",
        *,
        chunk_size: int = 320,
        total_chunks: int = 5,
        fail_with: TTSError | None = None,
        latency_ms: float = 0.0,
        is_healthy: bool = True,
        audio_pattern: bytes | None = None,
    ) -> None:
        self._name = name
        self.chunk_size = chunk_size
        self.total_chunks = total_chunks
        self.fail_with = fail_with
        self.latency_ms = latency_ms
        self.is_healthy = is_healthy
        self.audio_pattern = audio_pattern or (b"\x00\x01" * (chunk_size // 2))
        self.synthesize_call_count = 0
        self.last_language: str | None = None

    @property
    def name(self) -> str:
        return self._name

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        self.synthesize_call_count += 1
        self.last_language = language
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000.0)
        if self.fail_with:
            raise self.fail_with
        if not text.strip():
            return

        for _ in range(self.total_chunks):
            yield self.audio_pattern

    async def health(self) -> bool:
        return self.is_healthy and self.fail_with is None
