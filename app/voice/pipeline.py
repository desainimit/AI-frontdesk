import asyncio
import base64
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from app.ai.speech_text import speakable
from app.conversation.backchannel import is_backchannel
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState
from app.conversation.store import ConversationStateStore
from app.voice.audio import ULAW_FRAME, ulaw_speech
from app.voice.codec import ulaw_to_pcm
from app.voice.metrics import VoiceMetrics
from app.voice.processors.interruption import (
    ENERGY_REASON,
    InterruptionConfig,
    InterruptionHandler,
)
from app.voice.stt.base import SpeechToTextProvider, STTStream, TranscriptionResult
from app.voice.tts.base import TextToSpeechProvider

logger = logging.getLogger(__name__)

# Audio is sent this far ahead of what the caller hears. Twilio plays it from its buffer,
# so the next sentence can start without a gap, and `clear` still stops it at once.
LEAD_SECONDS = 0.4
# A mark Twilio never echoes back counts as played this long after its audio ended.
MARK_GRACE_SECONDS = 1.5
# A noise stopped the receptionist but no words followed: carry on after this long.
RESUME_AFTER_SECONDS = 1.5
# Words heard this uncertainly while the receptionist talks are taken for background.
BARGE_IN_CONFIDENCE = 0.5
# Silence after the receptionist finished, before it asks whether the caller is there.
SILENCE_SECONDS = 7.0


class PhraseStore(Protocol):
    async def get(self, voice: str, language: str, text: str) -> bytes | None: ...
    async def put(self, voice: str, language: str, text: str, audio: bytes) -> None: ...


class Speech:
    """The audio for one piece of text, made from the moment it is asked for.

    Preparing the next sentence while the current one plays is what removes the pause
    between them. Once sent, it knows when it started playing, so a sentence the caller
    cut short can be remembered as far as they heard it.
    """

    def __init__(
        self,
        text: str,
        source: AsyncIterator[bytes],
        on_complete: Callable[[bytes], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.text = text
        self.started_at: float | None = None
        self.duration = 0.0
        self.played = False
        self._queue: asyncio.Queue[bytes | BaseException | None] = asyncio.Queue()
        self._on_complete = on_complete
        self._task = asyncio.create_task(self._fill(source))

    @classmethod
    def ready(cls, text: str, audio: bytes) -> "Speech":
        async def frames() -> AsyncIterator[bytes]:
            for start in range(0, len(audio), ULAW_FRAME):
                yield audio[start : start + ULAW_FRAME]

        return cls(text, frames())

    async def _fill(self, source: AsyncIterator[bytes]) -> None:
        made = bytearray()
        try:
            async for frame in source:
                made.extend(frame)
                self._queue.put_nowait(frame)
        except asyncio.CancelledError:
            self._queue.put_nowait(None)
            raise
        except Exception as error:
            self._queue.put_nowait(error)
            return
        self._queue.put_nowait(None)
        if self._on_complete is not None:
            with suppress(Exception):
                await self._on_complete(bytes(made))

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def heard(self, at: float) -> str:
        """The words the caller heard if playback stopped at `at`."""
        if self.played:
            return self.text
        if self.started_at is None or self.duration <= 0 or at <= self.started_at:
            return ""
        words = self.text.split()
        share = min(1.0, (at - self.started_at) / self.duration)
        return " ".join(words[: int(len(words) * share)])

    def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()


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
        phrases: PhraseStore | None = None,
        on_resume: Callable[[], Coroutine[Any, Any, None]] | None = None,
        voice_id: str | None = None,
        on_silence: Callable[[int], Coroutine[Any, Any, None]] | None = None,
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
        self.phrases = phrases
        # Called when a noise stopped a reply and the caller then said nothing.
        self.on_resume = on_resume
        # The clinic's own voice, when it chose one.
        self.voice_id = voice_id
        # Called with 1, 2, ... for each stretch of silence in a row from the caller.
        self.on_silence = on_silence
        self._quiet_since = monotonic()
        self._silences = 0
        self._silence_task: asyncio.Task[None] | None = None

        self.interruption = InterruptionHandler(
            stream_sid=stream_sid,
            config=interruption_config,
            metrics=metrics,
            on_interrupt=self._handle_interruption_event,
        )

        self._stt_stream: STTStream | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._active_tts_task: asyncio.Task[bool] | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._active_response_task: asyncio.Task[None] | None = None
        self._resume_task: asyncio.Task[None] | None = None
        self._turns: asyncio.Queue[TranscriptionResult] = asyncio.Queue(maxsize=8)
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._running = False
        # Marks sent after each piece of speech, oldest first; Twilio echoes each one
        # back when that speech has finished playing.
        self._marks: OrderedDict[str, Speech] = OrderedDict()
        self._playback_deadline = 0.0
        # When the audio already sent to Twilio will have finished playing.
        self._play_clock = 0.0
        self._played = asyncio.Event()
        self._played.set()
        # A "mm-hmm" heard while the receptionist was talking, kept until it finishes.
        self._deferred: TranscriptionResult | None = None
        self.interrupted_at: float | None = None
        self.received_frames = 0
        self.sent_frames = 0
        self._last_audio_at: float | None = None
        self.transcript_history: list[TranscriptionResult] = []

    async def start(self) -> None:
        """Initializes STT stream and background transcription consumer."""
        self._running = True
        warm_up = getattr(self.tts, "warm_up", None)
        language = self.conversation_state.language if self.conversation_state else None
        if warm_up is not None:
            # The speech connection opens while recognition starts, not on the greeting.
            warming = asyncio.create_task(warm_up(language=language))
            self._stt_stream = await self.stt.create_stream(sample_rate=8000, encoding="linear16")
            with suppress(Exception):
                await warming
        else:
            self._stt_stream = await self.stt.create_stream(sample_rate=8000, encoding="linear16")
        self._stt_task = asyncio.create_task(self._consume_transcripts())
        self._turn_task = asyncio.create_task(self._consume_turns())
        if self.on_silence is not None:
            self._silence_task = asyncio.create_task(self._watch_silence())

    @property
    def _language(self) -> str:
        return self.conversation_state.language if self.conversation_state else "en"

    def _handle_interruption_event(self) -> None:
        """Synchronously called by InterruptionHandler when speech is detected."""
        if self._active_tts_task and not self._active_tts_task.done():
            self._active_tts_task.cancel()
        if self._active_response_task and not self._active_response_task.done():
            self._active_response_task.cancel()
        self._stop_playback()
        if self.interruption.last_reason == ENERGY_REASON and self.on_resume is not None:
            # Only a sound, no words yet: if none follow, the reply carries on.
            self._cancel_resume()
            self._resume_task = asyncio.create_task(self._resume_later())

    def _caller_active(self) -> None:
        self._quiet_since = monotonic()
        self._silences = 0

    async def _watch_silence(self) -> None:
        """Ask whether the caller is still there after a long silence, then let them go."""
        while self._running:
            await asyncio.sleep(0.25)
            if self.on_silence is None or not self._turns.empty() or self._deferred:
                continue
            if self._replying:
                # Talking is not silence; the clock starts when the reply has been heard.
                self._quiet_since = monotonic()
                continue
            if monotonic() - self._quiet_since < SILENCE_SECONDS:
                continue
            self._silences += 1
            self._quiet_since = monotonic()
            self._active_response_task = asyncio.create_task(self.on_silence(self._silences))

    async def _resume_later(self) -> None:
        await asyncio.sleep(RESUME_AFTER_SECONDS)
        if self.on_resume is None or not self._running or not self._turns.empty():
            return
        if self._active_response_task is not None and not self._active_response_task.done():
            return
        self._active_response_task = asyncio.create_task(self.on_resume())

    def _cancel_resume(self) -> None:
        if self._resume_task is not None and not self._resume_task.done():
            self._resume_task.cancel()
        self._resume_task = None

    @property
    def audio_pending(self) -> bool:
        """Whether Twilio may still be playing audio that was sent."""
        return bool(self._marks) or self._play_clock > monotonic()

    def _stop_playback(self) -> None:
        """Purge what Twilio has buffered but not yet played."""
        # Twilio echoes the marks of cleared audio too; they no longer mean "played".
        self._marks.clear()
        self._played.set()
        self.interrupted_at = monotonic()
        self._play_clock = monotonic()
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
        if self._marks and monotonic() >= self._playback_deadline:
            # Twilio did not echo the marks back; the audio has long since played.
            self._acknowledge(next(reversed(self._marks)))
        self.received_frames += 1
        pcm16 = ulaw_to_pcm(ulaw_audio)

        # Check for caller barge-in if TTS is currently synthesizing/playing
        if self.barge_in_enabled:
            self.interruption.process_incoming_audio(pcm16)
        else:
            self.interruption.hear(pcm16)
        if self.on_silence is not None and self.interruption.voiced:
            self._caller_active()

        # Forward audio to active STT stream
        if self._stt_stream:
            await self._stt_stream.send_audio(pcm16)
            self._last_audio_at = monotonic()

    @property
    def _replying(self) -> bool:
        active = self._active_response_task is not None and not self._active_response_task.done()
        return active or self.audio_pending

    def _heard_words(self, result: TranscriptionResult) -> None:
        """The caller is saying something while the receptionist talks."""
        self._cancel_resume()
        self._caller_active()
        if (
            self.barge_in_enabled
            and self.interruption.is_synthesizing
            # Background chatter is often half-heard: words the recognizer is unsure of
            # do not stop the receptionist; the caller's own clear words do.
            and result.confidence >= BARGE_IN_CONFIDENCE
            and not is_backchannel(result.text, self._language)
        ):
            self.interruption.trigger_interruption(reason="caller_words")

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
                if not result.text.strip():
                    continue
                if not result.is_final:
                    self._heard_words(result)
                    continue
                if self.metrics is not None and self._last_audio_at is not None:
                    self.metrics.record_stt_finalization_latency(
                        max(0.0, (monotonic() - self._last_audio_at) * 1000), self.stt.name
                    )
                self._cancel_resume()
                self._caller_active()
                if self._replying and is_backchannel(result.text, self._language):
                    # "Okay" while the receptionist is talking means "go on".
                    self._deferred = result
                    continue
                self._deferred = None
                if self._active_response_task and not self._active_response_task.done():
                    self._active_response_task.cancel()
                    if self.audio_pending:
                        # The caller said something new; what was still queued to
                        # play answers the old question.
                        self._stop_playback()
                self._turns.put_nowait(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("stt_stream_consumer_failed")
            raise RuntimeError("Speech stream failed") from None

    def _release_deferred(self) -> None:
        """A "yes" said over the end of a question is the answer to it; any other
        "mm-hmm" heard during a reply needed no answer."""
        deferred, self._deferred = self._deferred, None
        state = self.conversation_state
        if deferred is None or state is None:
            return
        asked = (state.last_agent_response or "").rstrip().endswith(("?", "?"))
        if asked or state.awaiting_confirmation:
            with suppress(asyncio.QueueFull):
                self._turns.put_nowait(deferred)

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
                if self._turns.empty():
                    self._release_deferred()
            finally:
                self._turns.task_done()

    async def prepare(
        self, text: str, voice_id: str | None = None, *, cache: bool = False
    ) -> Speech:
        """Start making the audio for `text` now; play it later with `play`.

        With `cache`, a phrase made on an earlier call is replayed instead of made again.
        """
        language = self.conversation_state.language if self.conversation_state else None
        voice_id = voice_id or self.voice_id
        voice = str(getattr(self.tts, "voice_key", self.tts.name)) + ":" + (voice_id or "")
        # Said the way a person reads it; the transcript keeps the written form.
        spoken = speakable(text, language)
        if cache and self.phrases is not None:
            audio = await self.phrases.get(voice, language or "", text)
            if audio:
                return Speech.ready(text, audio)
            phrases = self.phrases

            async def keep(made: bytes) -> None:
                await phrases.put(voice, language or "", text, made)

            return Speech(text, ulaw_speech(self.tts, spoken, voice_id, language), keep)
        return Speech(text, ulaw_speech(self.tts, spoken, voice_id, language))

    async def speak(
        self,
        text: str,
        voice_id: str | None = None,
        *,
        record: bool = True,
        follow_on: bool = False,
        cache: bool = False,
    ) -> bool:
        """Speak one piece of text. True when all of it was sent, false when the caller
        spoke over it or the call ended.

        With `record=False` the caller of this method keeps the transcript itself, as
        the receptionist does for a reply spoken sentence by sentence. `follow_on`
        continues what is already playing instead of cutting its last words off.
        """
        if not self._running or not text.strip():
            return False

        if record and self.conversation_state:
            ConversationStateManager.record_agent_response(self.conversation_state, text)
            self.conversation_state.append_transcript_turn("agent", text)
            if self.conversation_store:
                await self.conversation_store.save(
                    self.conversation_state, self.conversation_ttl_seconds
                )
        return await self.play(await self.prepare(text, voice_id, cache=cache), follow_on=follow_on)

    async def play(self, speech: Speech, *, follow_on: bool = False) -> bool:
        """Send prepared speech to the caller. True when all of it was sent."""
        if not self._running:
            speech.cancel()
            return False
        # Cancel any previous in-flight speech
        if self._active_tts_task and not self._active_tts_task.done():
            self._active_tts_task.cancel()

        if self._marks and not follow_on:
            self._stop_playback()
        self._active_tts_task = asyncio.create_task(self._stream(speech))
        try:
            return await self._active_tts_task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return False
        finally:
            speech.cancel()

    async def wait_played(self) -> None:
        """Wait until everything sent has been heard, or was cleared."""
        while self._marks and self._running:
            self._played.clear()
            timeout = max(0.05, self._playback_deadline - monotonic())
            try:
                async with asyncio.timeout(timeout):
                    await self._played.wait()
            except TimeoutError:
                if self._marks:
                    self._acknowledge(next(reversed(self._marks)))

    async def _pace(self) -> None:
        ahead = self._play_clock - monotonic()
        if ahead > LEAD_SECONDS:
            await asyncio.sleep(ahead - LEAD_SECONDS)

    async def _stream(self, speech: Speech) -> bool:
        self.interruption.set_synthesizing(True)
        completed = False
        first_chunk = True
        started = monotonic()
        try:
            async for frame in speech.frames():
                if self.interruption.is_interrupted or not self._running:
                    break
                if first_chunk:
                    if self.metrics is not None:
                        self.metrics.record_tts_pipeline_latency((monotonic() - started) * 1000)
                    first_chunk = False
                # Stay a little ahead of the caller, never far: what is buffered at
                # Twilio is what a barge-in has to clear.
                await self._pace()
                if self.interruption.is_interrupted or not self._running:
                    break
                payload = base64.b64encode(frame).decode("ascii")
                await self.send_frame(
                    {"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}}
                )
                self.sent_frames += 1
                begins = max(self._play_clock, monotonic())
                if speech.started_at is None:
                    speech.started_at = begins
                speech.duration += len(frame) / 8000
                self._play_clock = begins + len(frame) / 8000
            if not self.interruption.is_interrupted and self._running:
                name = "tts-" + uuid4().hex
                self._marks[name] = speech
                self._playback_deadline = max(monotonic(), self._play_clock) + MARK_GRACE_SECONDS
                await self.send_frame(
                    {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}
                )
                completed = True
        finally:
            if not completed and not self._marks:
                self.interruption.set_synthesizing(False)
        return completed

    def acknowledge_playback(self, name: str) -> None:
        if name in self._marks:
            self._acknowledge(name)

    def _acknowledge(self, name: str) -> None:
        # Twilio plays in order: this mark, and every one before it, has been heard.
        while self._marks:
            mark, speech = self._marks.popitem(last=False)
            speech.played = True
            if mark == name:
                break
        if not self._marks:
            self._played.set()
            if self._active_tts_task is None or self._active_tts_task.done():
                self.interruption.set_synthesizing(False)

    async def close(self) -> None:
        """Shuts down pipeline tasks and releases provider resources."""
        self._running = False
        self._played.set()
        tasks: list[asyncio.Task[Any]] = [
            task
            for task in (
                self._active_response_task,
                self._active_tts_task,
                self._stt_task,
                self._turn_task,
                self._resume_task,
                self._silence_task,
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
        for provider in (self.tts, self.stt):
            release = getattr(provider, "aclose", None)
            if release is not None:
                with suppress(Exception):
                    async with asyncio.timeout(5):
                        await release()
