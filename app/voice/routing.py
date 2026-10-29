"""Call-local routing using audio language evidence, never the previous turn alone."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import replace
from time import perf_counter

from app.conversation.locales import normalize_language
from app.languages import LANGUAGES, known
from app.languages import language as registry_language
from app.voice.audio import pcm_frames
from app.voice.codec import pcm_to_ulaw
from app.voice.stt.base import (
    SpeechToTextProvider,
    STTError,
    STTStream,
    STTTransientError,
    TranscriptionResult,
)
from app.voice.stt.utterance import UtteranceSTTStream
from app.voice.tts.base import TextToSpeechProvider, TTSError

logger = logging.getLogger(__name__)

# The speech locales Sarvam is built for: English in India, Hindi, Gujarati and so on.
SARVAM_LOCALES = frozenset(entry.sarvam for entry in LANGUAGES.values() if entry.sarvam)
# Opening Sarvam must not hold up the start of a call; Deepgram takes over after this.
SARVAM_OPEN_SECONDS = 2.5


class RoutedSTTProvider:
    """Azure detects every utterance; the selected service transcribes Gujarati.

    Deepgram uses an explicit Gujarati model request, not its multilingual mode.
    Auto mode sends audio to Azure for detection before transcription. Reusing the
    previous language would miss a caller's switch into Gujarati or back out of it.
    Fixed mode bypasses detection and sends audio only to the selected service.
    """

    def __init__(
        self,
        azure: SpeechToTextProvider,
        deepgram: SpeechToTextProvider,
        *,
        gujarati: SpeechToTextProvider | None = None,
        enabled_languages: tuple[str, ...],
        default_language: str,
        auto_detect: bool,
    ) -> None:
        self.azure, self.deepgram = azure, deepgram
        self.gujarati = gujarati if gujarati is not None else azure
        self.enabled_languages = enabled_languages
        self.default_language = default_language
        self.auto_detect = auto_detect and len(enabled_languages) > 1

    @property
    def name(self) -> str:
        return "language_router"

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        return UtteranceSTTStream(self, sample_rate, encoding)

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        started = perf_counter()
        if not self.auto_detect:
            provider = self.gujarati if self.default_language == "gu" else self.deepgram
            result = await provider.transcribe(audio, sample_rate)
            return replace(
                result, language=self.default_language, languages=(self.default_language,)
            )
        detected = await self.azure.transcribe(audio, sample_rate)
        if not detected.text.strip():
            return detected
        language = normalize_language(detected.language or "")
        if language not in self.enabled_languages:
            # Do not guess from the greeting or from text decoded with the wrong model.
            raise STTTransientError("Speech language could not be identified")
        if language == "gu" and self.gujarati is self.azure:
            return detected
        provider = self.gujarati if language == "gu" else self.deepgram
        result = await provider.transcribe(audio, sample_rate)
        return replace(
            result,
            confidence=min(result.confidence, detected.confidence),
            language=language,
            languages=(language,),
            duration_ms=(perf_counter() - started) * 1000,
        )

    async def health(self) -> bool:
        return (
            await self.azure.health()
            and await self.deepgram.health()
            and (self.gujarati is self.azure or await self.gujarati.health())
        )

    async def aclose(self) -> None:
        await _release(self.azure, self.deepgram, self.gujarati)


class RoutedTTSProvider:
    def __init__(self, azure: TextToSpeechProvider, elevenlabs: TextToSpeechProvider) -> None:
        self.azure, self.elevenlabs = azure, elevenlabs

    @property
    def name(self) -> str:
        return "language_router"

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        gujarati = normalize_language(language or "") == "gu"
        provider = self.azure if gujarati else self.elevenlabs
        # An ElevenLabs voice ID must never be sent to Azure.
        async for chunk in provider.synthesize(
            text, None if gujarati else voice_id, language=language
        ):
            yield chunk

    @property
    def voice_key(self) -> str:
        return "routed:" + str(getattr(self.elevenlabs, "voice_key", self.elevenlabs.name))

    async def warm_up(self, voice_id: str | None = None, *, language: str | None = None) -> None:
        warm_up = getattr(self.elevenlabs, "warm_up", None)
        if warm_up is not None and normalize_language(language or "") != "gu":
            await warm_up(voice_id, language=language)

    async def synthesize_ulaw(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        gujarati = normalize_language(language or "") == "gu"
        source = None if gujarati else getattr(self.elevenlabs, "synthesize_ulaw", None)
        if source is not None:
            async for chunk in source(text, voice_id, language=language):
                yield chunk
            return
        async for frame in pcm_frames(self.synthesize(text, voice_id, language=language)):
            yield pcm_to_ulaw(frame)

    async def health(self) -> bool:
        return await self.azure.health() and await self.elevenlabs.health()

    async def aclose(self) -> None:
        await _release(self.azure, self.elevenlabs)


def indian(locale: str) -> bool:
    """Whether Sarvam hears this speech locale: en-IN is Indian English, en-US is not."""
    return locale in SARVAM_LOCALES


class SarvamRouterSTTProvider:
    """Sarvam for the Indian languages, Deepgram for the rest, chosen per call language.

    The call's speech locale decides, read again for every audio frame, so a caller who
    moves from Hindi to Spanish moves to Deepgram on their next words. When Sarvam
    cannot be opened, stops, or fails, the rest of the call goes to Deepgram's own model
    for that language; the caller keeps talking and never hears an error.
    """

    def __init__(
        self,
        sarvam: SpeechToTextProvider,
        deepgram: SpeechToTextProvider,
        backup: Callable[[str], SpeechToTextProvider],
        locale: Callable[[], str],
    ) -> None:
        self.sarvam, self.deepgram = sarvam, deepgram
        self._backup = backup
        self._backups: dict[str, SpeechToTextProvider] = {}
        self.locale = locale
        # Set once Sarvam failed on this call; it is not tried again until the next call.
        self.sarvam_failed = False

    @property
    def name(self) -> str:
        return "sarvam_router"

    def backup(self, locale: str) -> SpeechToTextProvider:
        if locale not in self._backups:
            self._backups[locale] = self._backup(locale)
        return self._backups[locale]

    def route(self) -> tuple[str, SpeechToTextProvider]:
        """Where this call's audio goes now: a key naming the service, and the service."""
        locale = self.locale()
        if not indian(locale):
            return "deepgram", self.deepgram
        if not self.sarvam_failed:
            return "sarvam", self.sarvam
        return f"backup:{locale}", self.backup(locale)

    def sarvam_down(self, reason: str) -> None:
        if not self.sarvam_failed:
            self.sarvam_failed = True
            logger.warning("sarvam_recognition_failed_over", extra={"reason": reason})

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        stream = SarvamRouterStream(self, sample_rate, encoding)
        await stream.open()
        return stream

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        key, provider = self.route()
        if key != "sarvam":
            return await provider.transcribe(audio, sample_rate)
        try:
            return await provider.transcribe(audio, sample_rate)
        except STTError as error:
            self.sarvam_down(error.code)
        return await self.route()[1].transcribe(audio, sample_rate)

    async def health(self) -> bool:
        # Sarvam has a stand-in for every language it serves; Deepgram has none.
        return await self.deepgram.health()

    async def aclose(self) -> None:
        await _release(self.sarvam, self.deepgram, *self._backups.values())


class SarvamRouterStream:
    """One audio stream for the call, carried by whichever service the router names."""

    def __init__(self, router: SarvamRouterSTTProvider, sample_rate: int, encoding: str) -> None:
        self._router = router
        self._sample_rate, self._encoding = sample_rate, encoding
        self._results: asyncio.Queue[TranscriptionResult | STTError | None] = asyncio.Queue(
            maxsize=64
        )
        self._stream: STTStream | None = None
        self._key = ""
        # Bumped on every switch, so a stream that was let go cannot end the call.
        self._generation = 0
        self._pumps: set[asyncio.Task[None]] = set()
        self._switching = asyncio.Lock()
        self._closed = False

    async def open(self) -> None:
        async with self._switching:
            await self._switch()

    async def _connect(self, key: str, provider: SpeechToTextProvider) -> STTStream | None:
        """The new stream, or None when Sarvam could not open it (the router then names
        the stand-in). Any other service's failure is the caller's to handle."""
        if key != "sarvam":
            return await provider.create_stream(self._sample_rate, self._encoding)
        try:
            async with asyncio.timeout(SARVAM_OPEN_SECONDS):
                return await provider.create_stream(self._sample_rate, self._encoding)
        except TimeoutError:
            self._router.sarvam_down("stt_timeout")
        except STTError as error:
            self._router.sarvam_down(error.code)
        return None

    async def _switch(self) -> None:
        """Move to the service the router names now. Called holding self._switching."""
        key, provider = self._router.route()
        stream = await self._connect(key, provider)
        if stream is None:
            key, provider = self._router.route()
            stream = await self._connect(key, provider)
        assert stream is not None
        previous, self._stream, self._key = self._stream, stream, key
        self._generation += 1
        pump = asyncio.create_task(self._pump(stream, key, self._generation))
        self._pumps.add(pump)
        pump.add_done_callback(self._pumps.discard)
        if previous is not None:
            # Its last words still arrive; its errors no longer count.
            with suppress(Exception):
                await previous.close()

    async def _pump(self, stream: STTStream, key: str, generation: int) -> None:
        try:
            async for result in stream.results():
                await self._results.put(result)
        except asyncio.CancelledError:
            raise
        except STTError as error:
            await self._ended(key, generation, error)
            return
        except Exception:
            await self._ended(key, generation, STTTransientError("Speech recognition failed"))
            return
        await self._ended(key, generation, None)

    async def _ended(self, key: str, generation: int, error: STTError | None) -> None:
        if self._closed or generation != self._generation:
            return
        if key != "sarvam":
            await self._results.put(error)
            return
        # Sarvam stopped mid-call, with or without saying why: carry on elsewhere.
        self._router.sarvam_down(error.code if error else "stream_ended")
        try:
            async with self._switching:
                if generation == self._generation and not self._closed:
                    await self._switch()
        except STTError as failure:
            await self._results.put(failure)

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise STTTransientError("Speech stream is closed")
        if self._router.route()[0] != self._key:
            async with self._switching:
                if self._router.route()[0] != self._key:
                    await self._switch()
        stream, key = self._stream, self._key
        assert stream is not None
        try:
            await stream.send_audio(audio)
        except STTError as error:
            if key != "sarvam":
                raise
            self._router.sarvam_down(error.code)
            async with self._switching:
                if self._key == "sarvam":
                    await self._switch()
            assert self._stream is not None
            await self._stream.send_audio(audio)

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        while True:
            item = await self._results.get()
            if item is None:
                return
            if isinstance(item, STTError):
                raise item
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for pump in list(self._pumps):
            pump.cancel()
        if self._stream is not None:
            with suppress(Exception):
                await self._stream.close()
        with suppress(asyncio.QueueFull):
            self._results.put_nowait(None)


class SarvamRouterTTSProvider:
    """Sarvam's voices for the Indian languages, the ElevenLabs voice for the rest.

    English is Indian English, and spoken by Sarvam, only in a clinic in India. When
    Sarvam fails before any audio, the ElevenLabs voice says it instead if its model
    speaks that language.
    """

    def __init__(
        self,
        sarvam: TextToSpeechProvider,
        elevenlabs: TextToSpeechProvider,
        *,
        elevenlabs_model: str,
        country: str | None = None,
    ) -> None:
        self.sarvam, self.elevenlabs = sarvam, elevenlabs
        self.elevenlabs_model = elevenlabs_model
        self.country = country

    @property
    def name(self) -> str:
        return "sarvam_router"

    def _sarvam(self, language: str | None) -> bool:
        if not language or not known(language):
            return False
        return indian(registry_language(language).locale(self.country))

    def _elevenlabs_speaks(self, language: str | None) -> bool:
        if not language or not known(language):
            return True
        return self.elevenlabs_model in registry_language(language).elevenlabs_models

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        if not self._sarvam(language):
            async for chunk in self.elevenlabs.synthesize(text, voice_id, language=language):
                yield chunk
            return
        started = False
        try:
            # The clinic's ElevenLabs voice ID means nothing to Sarvam.
            async for chunk in self.sarvam.synthesize(text, None, language=language):
                started = True
                yield chunk
            return
        except TTSError as error:
            if started or not self._elevenlabs_speaks(language):
                raise
            logger.warning("sarvam_voice_failed_over", extra={"reason": error.code})
        async for chunk in self.elevenlabs.synthesize(text, voice_id, language=language):
            yield chunk

    @property
    def voice_key(self) -> str:
        speaker = str(getattr(self.sarvam, "speaker", ""))
        other = str(getattr(self.elevenlabs, "voice_key", self.elevenlabs.name))
        return f"sarvam_router:{speaker}:{other}"

    async def warm_up(self, voice_id: str | None = None, *, language: str | None = None) -> None:
        sarvam = self._sarvam(language)
        warm_up = getattr(self.sarvam if sarvam else self.elevenlabs, "warm_up", None)
        if warm_up is not None:
            await warm_up(None if sarvam else voice_id, language=language)

    async def synthesize_ulaw(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        source = (
            None if self._sarvam(language) else getattr(self.elevenlabs, "synthesize_ulaw", None)
        )
        if source is not None:
            async for chunk in source(text, voice_id, language=language):
                yield chunk
            return
        async for frame in pcm_frames(self.synthesize(text, voice_id, language=language)):
            yield pcm_to_ulaw(frame)

    async def health(self) -> bool:
        return await self.elevenlabs.health()

    async def aclose(self) -> None:
        await _release(self.sarvam, self.elevenlabs)


async def _release(*providers: object) -> None:
    for provider in providers:
        release = getattr(provider, "aclose", None)
        if release is not None:
            with suppress(Exception):
                await release()
