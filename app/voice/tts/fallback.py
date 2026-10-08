import logging
from collections.abc import AsyncIterator
from time import perf_counter

from app.voice.circuit import CircuitBreaker, CircuitState
from app.voice.metrics import VoiceMetrics
from app.voice.tts.base import TextToSpeechProvider, TTSError

logger = logging.getLogger(__name__)


class FallbackTTSProvider:
    """Orchestrates primary and fallback TTS providers with circuit breaking and metrics."""

    def __init__(
        self,
        primary: TextToSpeechProvider,
        fallback: TextToSpeechProvider,
        circuit_breaker: CircuitBreaker | None = None,
        metrics: VoiceMetrics | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.circuit = circuit_breaker or CircuitBreaker(
            name=f"tts_{primary.name}", failure_threshold=3, reset_seconds=60.0
        )
        self.metrics = metrics or VoiceMetrics()

    @property
    def name(self) -> str:
        return f"{self.primary.name}+{self.fallback.name}"

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        if not self.circuit.allow_request():
            self.metrics.record_fallback(
                "tts", self.primary.name, self.fallback.name, "circuit_open"
            )
            logger.info(
                "tts_circuit_open_using_fallback",
                extra={"primary": self.primary.name, "fallback": self.fallback.name},
            )
            started = perf_counter()
            first_chunk = True
            try:
                async for chunk in (
                    self.fallback.synthesize(text, language=language)
                    if language
                    else self.fallback.synthesize(text)
                ):
                    if first_chunk:
                        duration_ms = (perf_counter() - started) * 1000.0
                        self.metrics.record_tts_latency(duration_ms, self.fallback.name)
                        first_chunk = False
                    yield chunk
                return
            except TTSError as exc:
                self.metrics.record_tts_error(self.fallback.name, exc.code)
                raise

        # Attempt primary synthesis
        started = perf_counter()
        first_chunk = True
        yielded_primary_data = False
        try:
            async for chunk in (
                self.primary.synthesize(text, voice_id, language=language)
                if language
                else self.primary.synthesize(text, voice_id)
            ):
                if first_chunk:
                    duration_ms = (perf_counter() - started) * 1000.0
                    self.circuit.record_success()
                    self.metrics.record_tts_latency(duration_ms, self.primary.name)
                    first_chunk = False
                yielded_primary_data = True
                yield chunk
            return
        except TTSError as exc:
            duration_ms = (perf_counter() - started) * 1000.0
            self.circuit.record_failure()
            self.metrics.record_tts_error(self.primary.name, exc.code)
            if yielded_primary_data:
                # Never replay a sentence after part of it has already been spoken.
                raise
            self.metrics.record_fallback("tts", self.primary.name, self.fallback.name, exc.code)
            logger.warning(
                "tts_synthesize_fallback_triggered",
                extra={
                    "from_provider": self.primary.name,
                    "to_provider": self.fallback.name,
                    "reason": exc.code,
                },
            )

        # Failover to fallback
        fallback_started = perf_counter()
        fb_first_chunk = True
        try:
            async for chunk in (
                self.fallback.synthesize(text, language=language)
                if language
                else self.fallback.synthesize(text)
            ):
                if fb_first_chunk:
                    duration_ms = (perf_counter() - fallback_started) * 1000.0
                    self.metrics.record_tts_latency(duration_ms, self.fallback.name)
                    fb_first_chunk = False
                yield chunk
        except TTSError as exc:
            self.metrics.record_tts_error(self.fallback.name, exc.code)
            raise

    async def health(self) -> bool:
        primary_ok = (
            await self.primary.health() if self.circuit.state != CircuitState.OPEN else False
        )
        fallback_ok = await self.fallback.health()
        return primary_ok or fallback_ok
