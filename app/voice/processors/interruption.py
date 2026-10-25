import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.voice.codec import loudness
from app.voice.metrics import VoiceMetrics

logger = logging.getLogger(__name__)


ENERGY_REASON = "speech_energy_detected"


@dataclass
class InterruptionConfig:
    energy_threshold: float = 400.0  # RMS threshold for voice energy (silence is typically < 150)
    # Sound alone must last about 600 ms (30 frames of 20 ms) before it stops the
    # receptionist; a cough, a click or a short "mm" does not. Words the recognizer
    # hears stop it sooner (see VoicePipeline).
    consecutive_frames_required: int = 30


class InterruptionHandler:
    """Detects caller barge-in during speech synthesis and signals stream cancellation."""

    def __init__(
        self,
        stream_sid: str,
        *,
        config: InterruptionConfig | None = None,
        metrics: VoiceMetrics | None = None,
        on_interrupt: Callable[[], None] | None = None,
    ) -> None:
        self.stream_sid = stream_sid
        self.config = config or InterruptionConfig()
        self.metrics = metrics
        self.on_interrupt = on_interrupt
        self._is_synthesizing: bool = False
        self._consecutive_active_frames: int = 0
        self._interrupted_event = asyncio.Event()
        self.last_reason: str | None = None

    @property
    def is_synthesizing(self) -> bool:
        return self._is_synthesizing

    def set_synthesizing(self, active: bool) -> None:
        self._is_synthesizing = active
        if active:
            self._interrupted_event.clear()
            self._consecutive_active_frames = 0

    @property
    def is_interrupted(self) -> bool:
        return self._interrupted_event.is_set()

    def process_incoming_audio(self, pcm16: bytes) -> bool:
        """Analyzes incoming caller audio.

        If caller speaks while system is speaking, triggers barge-in.
        Returns True if barge-in was just triggered.
        """
        if not self._is_synthesizing or self._interrupted_event.is_set():
            return False

        if not pcm16:
            return False

        try:
            rms = loudness(pcm16)
        except Exception:
            return False

        if rms >= self.config.energy_threshold:
            self._consecutive_active_frames += 1
            if self._consecutive_active_frames >= self.config.consecutive_frames_required:
                self.trigger_interruption(reason=ENERGY_REASON)
                return True
        else:
            self._consecutive_active_frames = max(0, self._consecutive_active_frames - 1)

        return False

    def trigger_interruption(self, reason: str = "barge_in") -> None:
        """Instantly aborts current speech playback and marks interrupted state."""
        if self._interrupted_event.is_set():
            return

        self._interrupted_event.set()
        self._is_synthesizing = False
        self._consecutive_active_frames = 0
        self.last_reason = reason

        if self.metrics:
            self.metrics.record_interruption()

        logger.info(
            "caller_interruption_detected",
            extra={"stream_sid": self.stream_sid, "reason": reason},
        )

        if self.on_interrupt:
            try:
                self.on_interrupt()
            except Exception as exc:
                logger.error("error_in_interruption_callback", extra={"error": str(exc)})

    def twilio_clear_message(self) -> dict[str, str]:
        """Twilio Media Streams official command to immediately clear its output buffer."""
        return {"event": "clear", "streamSid": self.stream_sid}
