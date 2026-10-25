"""Call-local routing using audio language evidence, never the previous turn alone."""

from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import replace
from time import perf_counter

from app.conversation.locales import normalize_language
from app.voice.stt.base import (
    SpeechToTextProvider,
    STTStream,
    STTTransientError,
    TranscriptionResult,
)
from app.voice.stt.utterance import UtteranceSTTStream
from app.voice.tts.base import TextToSpeechProvider


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

    async def health(self) -> bool:
        return await self.azure.health() and await self.elevenlabs.health()

    async def aclose(self) -> None:
        await _release(self.azure, self.elevenlabs)


async def _release(*providers: object) -> None:
    for provider in providers:
        release = getattr(provider, "aclose", None)
        if release is not None:
            with suppress(Exception):
                await release()
