import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.voice.codec import loudness
from app.voice.metrics import VoiceMetrics

logger = logging.getLogger(__name__)


ENERGY_REASON = "speech_energy_detected"
# Speech must be this many times louder than the line's background noise. The noise
# level rises slowly in a noisy place (traffic, a TV) and falls quickly when it is quiet.
NOISE_MARGIN = 2.5
NOISE_RISE = 0.005
NOISE_FALL = 0.1
# However noisy the line, a caller speaking up still counts.
MAX_THRESHOLD = 3000.0


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
        # The line's background noise, learned from every frame the caller sends.
        self.noise_floor = 0.0
        # Whether the last frame sounded like someone speaking.
        self.voiced = False

    @property
    def threshold(self) -> float:
        """How loud a frame must be to count as speech on this line right now."""
        return min(
            MAX_THRESHOLD, max(self.config.energy_threshold, self.noise_floor * NOISE_MARGIN)
        )

    def hear(self, pcm16: bytes) -> bool:
        """Learn the background noise from this frame; True when it sounds like speech."""
        try:
            rms = loudness(pcm16) if pcm16 else 0.0
        except Exception:
            return False
        rate = NOISE_RISE if rms > self.noise_floor else NOISE_FALL
        self.noise_floor += (rms - self.noise_floor) * rate
        self.voiced = rms >= self.threshold
        return self.voiced

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
        voiced = self.hear(pcm16)
        if not self._is_synthesizing or self._interrupted_event.is_set():
            return False

        if voiced:
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
