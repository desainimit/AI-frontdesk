from opentelemetry.metrics import Counter, Histogram, Meter

from app.core.telemetry import Telemetry


class VoiceMetrics:
    """OpenTelemetry instrumentation for voice providers, latency, errors, and fallback."""

    def __init__(self, telemetry: Telemetry | None = None) -> None:
        meter: Meter | None = telemetry.metrics.get_meter("app.voice") if telemetry else None
        self.stt_latency: Histogram | None = (
            meter.create_histogram(
                "voice.stt.latency", unit="ms", description="STT request latency in milliseconds"
            )
            if meter
            else None
        )
        self.stt_error_total: Counter | None = (
            meter.create_counter("voice.stt.errors.total", description="Total STT provider errors")
            if meter
            else None
        )
        self.stt_finalization_latency: Histogram | None = (
            meter.create_histogram(
                "voice.stt.finalization.latency",
                unit="ms",
                description="Time from latest inbound frame to final live transcript",
            )
            if meter
            else None
        )
        self.llm_latency: Histogram | None = (
            meter.create_histogram(
                "voice.llm.latency", unit="ms", description="Structured LLM request latency"
            )
            if meter
            else None
        )
        self.tts_first_audio_latency: Histogram | None = (
            meter.create_histogram(
                "voice.tts.first_audio.latency",
                unit="ms",
                description="Time to first TTS audio chunk in milliseconds",
            )
            if meter
            else None
        )
        self.tts_pipeline_first_audio_latency: Histogram | None = (
            meter.create_histogram(
                "voice.tts.pipeline.first_audio.latency",
                unit="ms",
                description="Time from speak to first synthesized PCM chunk",
            )
            if meter
            else None
        )
        self.tts_error_total: Counter | None = (
            meter.create_counter("voice.tts.errors.total", description="Total TTS provider errors")
            if meter
            else None
        )
        self.provider_fallback_total: Counter | None = (
            meter.create_counter(
                "voice.provider.fallback.total",
                description="Total provider fallback events triggered",
            )
            if meter
            else None
        )
        self.emergency_detection_total: Counter | None = (
            meter.create_counter(
                "emergency_detection_total", description="Safety emergency overrides"
            )
            if meter
            else None
        )
        self.human_transfer_total: Counter | None = (
            meter.create_counter("human_transfer_total", description="Human escalation outcomes")
            if meter
            else None
        )
        self.call_interruption_total: Counter | None = (
            meter.create_counter(
                "voice.call.interruption.total",
                description="Total caller interruptions (barge-in)",
            )
            if meter
            else None
        )

    def record_stt_latency(self, duration_ms: float, provider: str) -> None:
        if self.stt_latency:
            self.stt_latency.record(duration_ms, {"provider": provider})

    def record_stt_error(self, provider: str, error_code: str) -> None:
        if self.stt_error_total:
            self.stt_error_total.add(1, {"provider": provider, "error_code": error_code})

    def record_stt_finalization_latency(self, duration_ms: float, provider: str) -> None:
        if self.stt_finalization_latency:
            self.stt_finalization_latency.record(duration_ms, {"provider": provider})

    def record_llm_latency(self, duration_ms: float, provider: str) -> None:
        if self.llm_latency:
            self.llm_latency.record(duration_ms, {"provider": provider})

    def record_tts_latency(self, duration_ms: float, provider: str) -> None:
        if self.tts_first_audio_latency:
            self.tts_first_audio_latency.record(duration_ms, {"provider": provider})

    def record_tts_pipeline_latency(self, duration_ms: float) -> None:
        if self.tts_pipeline_first_audio_latency:
            self.tts_pipeline_first_audio_latency.record(duration_ms)

    def record_tts_error(self, provider: str, error_code: str) -> None:
        if self.tts_error_total:
            self.tts_error_total.add(1, {"provider": provider, "error_code": error_code})

    def record_fallback(
        self, provider_type: str, from_provider: str, to_provider: str, reason: str
    ) -> None:
        if self.provider_fallback_total:
            self.provider_fallback_total.add(
                1,
                {
                    "provider_type": provider_type,
                    "from_provider": from_provider,
                    "to_provider": to_provider,
                    "reason": reason,
                },
            )

    def record_interruption(self) -> None:
        if self.call_interruption_total:
            self.call_interruption_total.add(1)

    def record_emergency(self, source: str) -> None:
        if self.emergency_detection_total:
            self.emergency_detection_total.add(1, {"source": source})

    def record_human_transfer(self, outcome: str) -> None:
        if self.human_transfer_total:
            self.human_transfer_total.add(1, {"outcome": outcome})
