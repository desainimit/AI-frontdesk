import asyncio
import audioop
import base64
import logging
from collections.abc import Callable, Coroutine
from time import monotonic
from typing import Any
from uuid import uuid4

from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState
from app.conversation.store import ConversationStateStore
from app.voice.audio import pcm_frames
from app.voice.metrics import VoiceMetrics
from app.voice.processors.interruption import InterruptionConfig, InterruptionHandler
from app.voice.stt.base import SpeechToTextProvider, STTStream, TranscriptionResult
from app.voice.tts.base import TextToSpeechProvider

logger = logging.getLogger(__name__)


class VoicePipeline:
    """Manages real-time audio flow: inbound audio -> STT -> Agent/Response -> TTS -> Twilio,

    with barge-in interruption detection and resilient provider fallback.
    """

    def __init__(
        self,
        stream_sid: str,
        stt_provider: SpeechToTextProvider,
        tts_provider: TextToSpeechProvider,
        send_frame: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
        *,
        interruption_config: InterruptionConfig | None = None,
        metrics: VoiceMetrics | None = None,
        on_transcript: Callable[[TranscriptionResult], Coroutine[Any, Any, None]] | None = None,
        conversation_state: ConversationState | None = None,
        conversation_store: ConversationStateStore | None = None,
        conversation_ttl_seconds: int = 3600,
        language_threshold: float = 0.6,
        multilingual_enabled: bool = False,
        barge_in_enabled: bool = True,
    ) -> None:
        self.stream_sid = stream_sid
        self.stt = stt_provider
        self.tts = tts_provider
        self.send_frame = send_frame
        self.metrics = metrics
        self.on_transcript = on_transcript
        self.conversation_state = conversation_state
        self.conversation_store = conversation_store
        self.conversation_ttl_seconds = conversation_ttl_seconds
        self.language_threshold = language_threshold
        self.multilingual_enabled = multilingual_enabled
        self.barge_in_enabled = barge_in_enabled

        self.interruption = InterruptionHandler(
            stream_sid=stream_sid,
            config=interruption_config,
            metrics=metrics,
            on_interrupt=self._handle_interruption_event,
        )

        self._stt_stream: STTStream | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._active_tts_task: asyncio.Task[None] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._active_response_task: asyncio.Task[None] | None = None
        self._turns: asyncio.Queue[TranscriptionResult] = asyncio.Queue(maxsize=8)
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._running = False
        self._playback_mark: str | None = None
        self._playback_deadline = 0.0
        self.received_frames = 0
        self.sent_frames = 0
        self._last_audio_at: float | None = None
        self.transcript_history: list[TranscriptionResult] = []

    async def start(self) -> None:
        """Initializes STT stream and background transcription consumer."""
        self._running = True
        self._stt_stream = await self.stt.create_stream(sample_rate=8000, encoding="linear16")
        self._stt_task = asyncio.create_task(self._consume_transcripts())
        self._turn_task = asyncio.create_task(self._consume_turns())

    def _handle_interruption_event(self) -> None:
        """Synchronously called by InterruptionHandler when speech is detected."""
        self._playback_mark = None
        if self._active_tts_task and not self._active_tts_task.done():
            self._active_tts_task.cancel()
        if self._active_response_task and not self._active_response_task.done():
            self._active_response_task.cancel()

        # Emit the clear message asynchronously to purge Twilio's audio buffer
        task = asyncio.create_task(self._send_clear())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _send_clear(self) -> None:
        try:
            await self.send_frame(self.interruption.twilio_clear_message())
        except Exception:
            logger.warning("voice_clear_failed")

    async def process_inbound_audio(self, ulaw_audio: bytes) -> None:
        """Processes an incoming µ-law audio frame from the telephony provider."""
        if not self._running:
            return
        self.check_tasks()
        if self._playback_mark and monotonic() >= self._playback_deadline:
            self._playback_mark = None
            self.interruption.set_synthesizing(False)
        self.received_frames += 1
        pcm16 = audioop.ulaw2lin(ulaw_audio, 2)

        # Check for caller barge-in if TTS is currently synthesizing/playing
        if self.barge_in_enabled:
            self.interruption.process_incoming_audio(pcm16)

        # Forward audio to active STT stream
        if self._stt_stream:
            await self._stt_stream.send_audio(pcm16)
            self._last_audio_at = monotonic()

    async def _consume_transcripts(self) -> None:
        """Continuously reads transcription results from the STT stream."""
        if not self._stt_stream:
            return
        try:
            async for result in self._stt_stream.results():
                if not self._running:
                    break
                self.transcript_history.append(result)
                del self.transcript_history[:-20]
                if result.is_final and result.text.strip():
                    if self.metrics is not None and self._last_audio_at is not None:
                        self.metrics.record_stt_finalization_latency(
                            max(0.0, (monotonic() - self._last_audio_at) * 1000), self.stt.name
                        )
                    if self._active_response_task and not self._active_response_task.done():
                        self._active_response_task.cancel()
                    self._turns.put_nowait(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("stt_stream_consumer_failed")
            raise RuntimeError("Speech stream failed") from None

    def check_tasks(self) -> None:
        for task in (self._stt_task, self._turn_task):
            if task is not None and task.done() and not task.cancelled():
                task.result()

    async def _consume_turns(self) -> None:
        while self._running:
            result = await self._turns.get()
            try:
                if self.conversation_state:
                    ConversationStateManager.process_caller_utterance(
                        self.conversation_state,
                        result.text,
                        detected_language=result.language,
                        detected_languages=result.languages,
                        confidence=result.confidence,
                        language_threshold=self.language_threshold,
                    )
                    if self.conversation_store:
                        await self.conversation_store.save(
                            self.conversation_state, self.conversation_ttl_seconds
                        )
                if self.on_transcript:
                    self._active_response_task = asyncio.create_task(self.on_transcript(result))
                    try:
                        await self._active_response_task
                    except asyncio.CancelledError:
                        if not self._running:
                            raise
            finally:
                self._turns.task_done()

    async def speak(self, text: str, voice_id: str | None = None) -> None:
        """Synthesizes text into streaming audio and sends µ-law frames to the caller."""
        if not self._running or not text.strip():
            return

        if self.conversation_state:
            ConversationStateManager.record_agent_response(self.conversation_state, text)
            self.conversation_state.append_transcript_turn("agent", text)
            if self.conversation_store:
                await self.conversation_store.save(
                    self.conversation_state, self.conversation_ttl_seconds
                )

        # Cancel any previous in-flight speech
        if self._active_tts_task and not self._active_tts_task.done():
            self._active_tts_task.cancel()

        if self._playback_mark is not None:
            self._playback_mark = None
            await self._send_clear()
        self._active_tts_task = asyncio.create_task(self._stream_tts(text, voice_id))
        try:
            await self._active_tts_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

    async def _stream_tts(self, text: str, voice_id: str | None) -> None:
        self.interruption.set_synthesizing(True)
        completed = False
        first_chunk = True
        duration = 0.0
        started = monotonic()
        try:
            speech = (
                self.tts.synthesize(text, voice_id, language=self.conversation_state.language)
                if self.multilingual_enabled and self.conversation_state
                else self.tts.synthesize(text, voice_id)
            )
            async for pcm_chunk in pcm_frames(speech):
                if self.interruption.is_interrupted or not self._running:
                    break
                if first_chunk:
                    if self.metrics is not None:
                        self.metrics.record_tts_pipeline_latency((monotonic() - started) * 1000)
                    first_chunk = False

                # Twilio expects 8000Hz 8-bit µ-law frames
                ulaw_chunk = audioop.lin2ulaw(pcm_chunk, 2)
                payload = base64.b64encode(ulaw_chunk).decode("ascii")
                message = {
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": payload},
                }
                await self.send_frame(message)
                self.sent_frames += 1
                duration += len(pcm_chunk) / 16000
                # Pace by audio duration, not by arbitrary HTTP chunk count.
                await asyncio.sleep(len(pcm_chunk) / 16000)
            if not self.interruption.is_interrupted and self._running:
                self._playback_mark = "tts-" + uuid4().hex
                self._playback_deadline = max(monotonic(), started + duration) + 5
                await self.send_frame(
                    {
                        "event": "mark",
                        "streamSid": self.stream_sid,
                        "mark": {"name": self._playback_mark},
                    }
                )
                completed = True
        finally:
            if not completed:
                self.interruption.set_synthesizing(False)

    def acknowledge_playback(self, name: str) -> None:
        if name == self._playback_mark:
            self._playback_mark = None
            self.interruption.set_synthesizing(False)

    async def close(self) -> None:
        """Shuts down pipeline tasks and releases provider resources."""
        self._running = False
        tasks = [
            task
            for task in (
                self._active_response_task,
                self._active_tts_task,
                self._stt_task,
                self._turn_task,
            )
            if task is not None
        ]
        tasks.extend(self._background_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._stt_stream:
            async with asyncio.timeout(5):
                await self._stt_stream.close()
