import asyncio
from collections.abc import AsyncIterator

from app.voice.stt.base import (
    STTError,
    STTStream,
    TranscriptionResult,
)


class MockSTTStream:
    def __init__(
        self,
        results: list[TranscriptionResult] | None = None,
        fail_with: STTError | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._results = (
            list(results)
            if results is not None
            else [
                TranscriptionResult(
                    text="Hello frontdesk", is_final=True, confidence=0.98, language="en"
                )
            ]
        )
        self._fail_with = fail_with
        self._delay = delay_seconds
        self._queue: asyncio.Queue[TranscriptionResult | None] = asyncio.Queue()
        self._closed = False
        self._audio_bytes = 0

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise RuntimeError("Cannot send audio to a closed stream")
        if self._fail_with:
            raise self._fail_with
        self._audio_bytes += len(audio)
        if self._results and self._audio_bytes >= 320:  # ~20ms of 8kHz 16-bit PCM
            next_result = self._results.pop(0)
            await self._queue.put(next_result)

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            while self._audio_bytes > 0 and self._results:
                await self._queue.put(self._results.pop(0))
            await self._queue.put(None)

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        while True:
            if self._delay > 0:
                await asyncio.sleep(self._delay)
            if self._fail_with:
                raise self._fail_with
            item = await self._queue.get()
            if item is None:
                break
            yield item


class MockSTTProvider:
    """Deterministic, mockable STT provider for unit and scenario testing."""

    def __init__(
        self,
        name: str = "mock",
        *,
        results: list[TranscriptionResult] | None = None,
        fail_with: STTError | None = None,
        latency_ms: float = 0.0,
        is_healthy: bool = True,
    ) -> None:
        self._name = name
        self.results_script = (
            list(results)
            if results is not None
            else [
                TranscriptionResult(
                    text="Test appointment booking", is_final=True, confidence=0.99, language="en"
                )
            ]
        )
        self.fail_with = fail_with
        self.latency_ms = latency_ms
        self.is_healthy = is_healthy
        self.transcribe_call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        if self.fail_with:
            raise self.fail_with
        return MockSTTStream(
            results=list(self.results_script),
            fail_with=self.fail_with,
            delay_seconds=self.latency_ms / 1000.0,
        )

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        self.transcribe_call_count += 1
        if self.latency_ms > 0:
            await asyncio.sleep(self.latency_ms / 1000.0)
        if self.fail_with:
            raise self.fail_with
        if self.results_script:
            return self.results_script.pop(0)
        return TranscriptionResult(
            text="Mock transcribed audio", is_final=True, confidence=0.95, language="en"
        )

    async def health(self) -> bool:
        return self.is_healthy and self.fail_with is None
