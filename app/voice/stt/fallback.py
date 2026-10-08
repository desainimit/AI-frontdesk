import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from time import perf_counter

from app.voice.circuit import CircuitBreaker, CircuitState
from app.voice.metrics import VoiceMetrics
from app.voice.stt.base import (
    SpeechToTextProvider,
    STTError,
    STTStream,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


class FallbackSTTStream:
    """Switches once, closes the old stream and replays at most two seconds once."""

    def __init__(
        self,
        primary_stream: STTStream,
        fallback_provider: SpeechToTextProvider,
        primary_name: str,
        sample_rate: int,
        encoding: str,
        circuit: CircuitBreaker,
        metrics: VoiceMetrics | None = None,
    ) -> None:
        self._current_stream = primary_stream
        self._fallback_provider = fallback_provider
        self._primary_name = primary_name
        self._sample_rate = sample_rate
        self._encoding = encoding
        self._circuit = circuit
        self._metrics = metrics
        self._using_fallback = False
        self._closed = False
        self._buffered_audio = bytearray()
        self._switch_lock = asyncio.Lock()

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise STTError("Speech stream is closed")
        async with self._switch_lock:
            source = self._current_stream
            self._buffered_audio.extend(audio)
            del self._buffered_audio[: -self._sample_rate * 4]
            try:
                await source.send_audio(audio)
            except STTError as error:
                await self._switch_locked(source, error)
                # Replay already includes this packet; never send it twice.

    async def _switch_locked(self, source: STTStream, error: STTError) -> None:
        if source is not self._current_stream:
            return
        if self._using_fallback or self._closed:
            raise error
        self._using_fallback = True
        self._circuit.record_failure()
        if self._metrics:
            self._metrics.record_stt_error(self._primary_name, error.code)
            self._metrics.record_fallback(
                "stt", self._primary_name, self._fallback_provider.name, error.code
            )
        logger.warning(
            "stt_stream_fallback_triggered",
            extra={
                "from_provider": self._primary_name,
                "to_provider": self._fallback_provider.name,
                "reason": error.code,
            },
        )
        # Teardown must complete before starting a second provider.
        with suppress(STTError, TimeoutError):
            async with asyncio.timeout(5):
                await source.close()
        self._current_stream = await self._fallback_provider.create_stream(
            self._sample_rate, self._encoding
        )
        replay = bytes(self._buffered_audio)
        self._buffered_audio.clear()
        if replay:
            await self._current_stream.send_audio(replay)

    async def close(self) -> None:
        self._closed = True
        async with self._switch_lock:
            await self._current_stream.close()
        self._buffered_audio.clear()

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        while True:
            source = self._current_stream
            try:
                async for result in source.results():
                    if source is not self._current_stream:
                        break
                    if result.is_final:
                        self._buffered_audio.clear()
                        if not self._using_fallback:
                            self._circuit.record_success()
                    yield result
                async with self._switch_lock:
                    if source is self._current_stream:
                        return
            except STTError as error:
                async with self._switch_lock:
                    await self._switch_locked(source, error)


class FallbackSTTProvider:
    """Orchestrates primary and fallback STT providers with health tracking and circuit breaking."""

    def __init__(
        self,
        primary: SpeechToTextProvider,
        fallback: SpeechToTextProvider,
        circuit_breaker: CircuitBreaker | None = None,
        metrics: VoiceMetrics | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.circuit = circuit_breaker or CircuitBreaker(
            name=f"stt_{primary.name}", failure_threshold=3, reset_seconds=60.0
        )
        self.metrics = metrics or VoiceMetrics()

    @property
    def name(self) -> str:
        return f"{self.primary.name}+{self.fallback.name}"

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        if not self.circuit.allow_request():
            self.metrics.record_fallback(
                "stt", self.primary.name, self.fallback.name, "circuit_open"
            )
            logger.info(
                "stt_circuit_open_using_fallback",
                extra={"primary": self.primary.name, "fallback": self.fallback.name},
            )
            return await self.fallback.create_stream(sample_rate, encoding)

        try:
            stream = await self.primary.create_stream(sample_rate, encoding)
            return FallbackSTTStream(
                primary_stream=stream,
                fallback_provider=self.fallback,
                primary_name=self.primary.name,
                sample_rate=sample_rate,
                encoding=encoding,
                circuit=self.circuit,
                metrics=self.metrics,
            )
        except STTError as exc:
            self.circuit.record_failure()
            self.metrics.record_stt_error(self.primary.name, exc.code)
            self.metrics.record_fallback("stt", self.primary.name, self.fallback.name, exc.code)
            logger.warning(
                "stt_stream_creation_fallback_triggered",
                extra={
                    "from_provider": self.primary.name,
                    "to_provider": self.fallback.name,
                    "reason": exc.code,
                },
            )
            return await self.fallback.create_stream(sample_rate, encoding)

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        if not self.circuit.allow_request():
            self.metrics.record_fallback(
                "stt", self.primary.name, self.fallback.name, "circuit_open"
            )
            started = perf_counter()
            try:
                res = await self.fallback.transcribe(audio, sample_rate)
                duration_ms = (perf_counter() - started) * 1000.0
                self.metrics.record_stt_latency(duration_ms, self.fallback.name)
                return res
            except STTError as exc:
                self.metrics.record_stt_error(self.fallback.name, exc.code)
                raise

        # Circuit allows request
        started = perf_counter()
        try:
            res = await self.primary.transcribe(audio, sample_rate)
            duration_ms = (perf_counter() - started) * 1000.0
            self.circuit.record_success()
            self.metrics.record_stt_latency(duration_ms, self.primary.name)
            return res
        except STTError as exc:
            duration_ms = (perf_counter() - started) * 1000.0
            self.circuit.record_failure()
            self.metrics.record_stt_error(self.primary.name, exc.code)
            self.metrics.record_fallback("stt", self.primary.name, self.fallback.name, exc.code)
            logger.warning(
                "stt_transcribe_fallback_triggered",
                extra={
                    "from_provider": self.primary.name,
                    "to_provider": self.fallback.name,
                    "reason": exc.code,
                },
            )

        # Fallback invocation
        fallback_started = perf_counter()
        try:
            res = await self.fallback.transcribe(audio, sample_rate)
            fallback_duration_ms = (perf_counter() - fallback_started) * 1000.0
            self.metrics.record_stt_latency(fallback_duration_ms, self.fallback.name)
            return res
        except STTError as exc:
            self.metrics.record_stt_error(self.fallback.name, exc.code)
            raise

    async def health(self) -> bool:
        primary_ok = (
            await self.primary.health() if self.circuit.state != CircuitState.OPEN else False
        )
        fallback_ok = await self.fallback.health()
        return primary_ok or fallback_ok
