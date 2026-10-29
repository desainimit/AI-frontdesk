"""Sarvam for Indian languages, Deepgram and ElevenLabs for the rest, and Sarvam's
stand-ins when it fails. Every service here is a fake: nothing is billed."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.conversation.locales import LAUNCH_LANGUAGES, LanguagePolicy
from app.core.settings import Settings
from app.integrations.catalog import spec_for, validate_config
from app.integrations.resolver import ResolvedIntegration
from app.platform.support import SUPPORT_ROUTES
from app.tenants.readiness import _stt_key, _tts_key  # pyright: ignore[reportPrivateUsage]
from app.voice.factory import stt_from_connection, tts_from_connection, validate_voices
from app.voice.routing import SarvamRouterSTTProvider, SarvamRouterTTSProvider
from app.voice.stt.base import STTStream, STTTransientError, TranscriptionResult
from app.voice.tts.base import TTSTransientError
from app.voice.tts.sarvam import SarvamTTSProvider


class FakeStream:
    def __init__(self, name: str) -> None:
        self.name = name
        self.audio: list[bytes] = []
        self.closed = False
        self.queue: asyncio.Queue[TranscriptionResult | Exception | None] = asyncio.Queue()

    async def send_audio(self, audio: bytes) -> None:
        if self.closed:
            raise STTTransientError("closed")
        self.audio.append(audio)

    def say(self, text: str) -> None:
        self.queue.put_nowait(TranscriptionResult(text, True, language=self.name))

    def fail(self) -> None:
        self.queue.put_nowait(STTTransientError("stopped"))

    async def close(self) -> None:
        self.closed = True
        self.queue.put_nowait(None)

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item


class FakeSTT:
    def __init__(self, name: str, refuse: bool = False) -> None:
        self._name = name
        self.refuse = refuse
        self.streams: list[FakeStream] = []

    @property
    def name(self) -> str:
        return self._name

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        if self.refuse:
            raise STTTransientError("cannot open")
        self.streams.append(FakeStream(self._name))
        return self.streams[-1]

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        if self.refuse:
            raise STTTransientError("cannot answer")
        return TranscriptionResult(self._name, True)

    async def health(self) -> bool:
        return True


def router(locale: list[str], sarvam_refuses: bool = False) -> tuple[Any, ...]:
    sarvam, deepgram = FakeSTT("sarvam", sarvam_refuses), FakeSTT("deepgram")
    backups: dict[str, FakeSTT] = {}

    def backup(speech_locale: str) -> FakeSTT:
        backups[speech_locale] = FakeSTT(f"backup-{speech_locale}")
        return backups[speech_locale]

    return (
        SarvamRouterSTTProvider(sarvam, deepgram, backup, lambda: locale[0]),
        sarvam,
        deepgram,
        backups,
    )


async def next_result(stream: STTStream) -> TranscriptionResult:
    async with asyncio.timeout(1):
        return await anext(aiter(stream.results()))


@pytest.mark.parametrize(
    ("locale", "service"),
    [("hi-IN", "sarvam"), ("gu-IN", "sarvam"), ("en-IN", "sarvam"), ("en-US", "deepgram")],
)
async def test_indian_speech_goes_to_sarvam_and_the_rest_to_deepgram(
    locale: str, service: str
) -> None:
    provider, sarvam, deepgram, _ = router([locale])
    stream = await provider.create_stream()
    await stream.send_audio(b"\0\0")
    used = sarvam if service == "sarvam" else deepgram
    other = deepgram if service == "sarvam" else sarvam
    assert used.streams[0].audio == [b"\0\0"] and not other.streams
    await stream.close()


async def test_a_caller_who_changes_language_moves_service_and_keeps_their_words() -> None:
    locale = ["hi-IN"]
    provider, sarvam, deepgram, _ = router(locale)
    stream = await provider.create_stream()
    await stream.send_audio(b"\0\0")
    sarvam.streams[0].say("नमस्ते")
    assert (await next_result(stream)).text == "नमस्ते"
    locale[0] = "es-ES"
    await stream.send_audio(b"\1\1")
    assert sarvam.streams[0].closed and deepgram.streams[0].audio == [b"\1\1"]
    deepgram.streams[0].say("hola")
    assert (await next_result(stream)).text == "hola"
    await stream.close()


async def test_sarvam_that_cannot_open_hands_the_call_to_deepgram() -> None:
    provider, _, _, backups = router(["gu-IN"], sarvam_refuses=True)
    stream = await provider.create_stream()
    await stream.send_audio(b"\0\0")
    # Gujarati goes to Deepgram's own Gujarati model, not its multilingual one.
    assert backups["gu-IN"].streams[0].audio == [b"\0\0"]
    assert provider.sarvam_failed
    await stream.close()


async def test_sarvam_failing_mid_call_is_never_heard_by_the_caller() -> None:
    provider, sarvam, _, backups = router(["hi-IN"])
    stream = await provider.create_stream()
    sarvam.streams[0].fail()
    async with asyncio.timeout(1):
        while "hi-IN" not in backups or not backups["hi-IN"].streams:  # noqa: ASYNC110
            await asyncio.sleep(0)
    backups["hi-IN"].streams[0].say("जी हाँ")
    assert (await next_result(stream)).text == "जी हाँ"
    # Sarvam is not tried again on this call.
    await stream.send_audio(b"\0\0")
    assert len(sarvam.streams) == 1 and backups["hi-IN"].streams[0].audio == [b"\0\0"]
    await stream.close()


async def test_deepgram_failing_is_reported_not_hidden() -> None:
    provider, _, deepgram, _ = router(["en-US"])
    stream = await provider.create_stream()
    deepgram.streams[0].fail()
    with pytest.raises(STTTransientError):
        await next_result(stream)
    await stream.close()


async def test_short_audio_falls_back_too() -> None:
    provider, _, _, _ = router(["hi-IN"], sarvam_refuses=True)
    assert (await provider.transcribe(b"\0\0")).text == "backup-hi-IN"


class FakeTTS:
    def __init__(self, name: str, fail: bool = False) -> None:
        self._name = name
        self.fail = fail
        self.calls: list[tuple[str | None, str | None]] = []

    @property
    def name(self) -> str:
        return self._name

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        self.calls.append((voice_id, language))
        if self.fail:
            raise TTSTransientError("down")
        yield self._name.encode()

    async def health(self) -> bool:
        return True


async def spoken(provider: Any, language: str) -> bytes:
    return b"".join(
        [chunk async for chunk in provider.synthesize("hi", "eleven-voice", language=language)]
    )


@pytest.mark.parametrize(
    ("language", "country", "voice"),
    [
        ("hi", None, "sarvam"),
        ("hi-Latn", "US", "sarvam"),
        ("gu", "IN", "sarvam"),
        ("en", "IN", "sarvam"),
        ("en", "US", "elevenlabs"),
        ("es", "IN", "elevenlabs"),
    ],
)
async def test_voice_follows_the_reply_language(
    language: str, country: str | None, voice: str
) -> None:
    sarvam, elevenlabs = FakeTTS("sarvam"), FakeTTS("elevenlabs")
    provider = SarvamRouterTTSProvider(
        sarvam, elevenlabs, elevenlabs_model="eleven_flash_v2_5", country=country
    )
    assert await spoken(provider, language) == voice.encode()
    # The ElevenLabs voice ID never goes to Sarvam.
    assert sarvam.calls in ([], [(None, language)])


async def test_elevenlabs_says_it_when_sarvam_fails_and_can_speak_the_language() -> None:
    provider = SarvamRouterTTSProvider(
        FakeTTS("sarvam", fail=True), FakeTTS("elevenlabs"), elevenlabs_model="eleven_flash_v2_5"
    )
    assert await spoken(provider, "hi") == b"elevenlabs"
    # Flash 2.5 has no Gujarati: the failure is reported, not spoken in the wrong voice.
    with pytest.raises(TTSTransientError):
        await spoken(provider, "gu")


class FakeSocket:
    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = [json.dumps(reply) for reply in replies]
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> str:
        return self.replies.pop(0)

    async def close(self) -> None:
        self.closed = True


def audio(samples: bytes) -> dict[str, Any]:
    return {"type": "audio", "data": {"audio": base64.b64encode(samples).decode()}}


FINAL = {"type": "event", "data": {"event_type": "final"}}


async def test_sarvam_voice_streams_and_keeps_its_socket_between_sentences() -> None:
    sockets = [FakeSocket([audio(b"\1\2\3"), audio(b"\4"), FINAL, audio(b"\5\6"), FINAL])]
    opened: list[str] = []

    async def connector(url: str, **_: Any) -> FakeSocket:
        opened.append(url)
        return sockets[0]

    voice = SarvamTTSProvider(SecretStr("k" * 20), connector=connector)
    first = [chunk async for chunk in voice.synthesize("नमस्ते", "eleven", language="hi")]
    # A sample split across two pieces is joined, never played as noise.
    assert first == [b"\1\2", b"\3\4"]
    second = [chunk async for chunk in voice.synthesize("जी", language="gu")]
    assert second == [b"\5\6"] and len(opened) == 1 and "bulbul%3Av3" in opened[0]
    config = sockets[0].sent[0]["data"]
    assert config["speech_sample_rate"] == 8000 and config["output_audio_codec"] == "linear16"
    assert config["speaker"] == "eleven" and sockets[0].sent[3]["data"]["language_code"] == "gu-IN"
    await voice.aclose()
    assert sockets[0].closed


async def test_sarvam_voice_falls_back_to_rest_when_the_stream_refuses() -> None:
    socket = FakeSocket([{"type": "error", "data": {"message": "bad config", "code": 422}}])

    async def connector(url: str, **_: Any) -> FakeSocket:
        return socket

    wav = base64.b64encode(_wav(b"\7\7")).decode()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"audios": [wav]}))
    )
    voice = SarvamTTSProvider(SecretStr("k" * 20), connector=connector, client=client)
    assert [chunk async for chunk in voice.synthesize("hello", language="en")] == [b"\7\7"]
    assert socket.closed and not voice.streaming
    await client.aclose()


def _wav(samples: bytes) -> bytes:
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(samples)
    return buffer.getvalue()


def connection(purpose: str, **config: Any) -> ResolvedIntegration:
    other = "deepgram_api_key" if purpose == "stt" else "elevenlabs_api_key"
    return ResolvedIntegration(
        uuid4(),
        uuid4(),
        uuid4(),
        "sarvam_router",
        purpose,
        "primary",
        None,
        validate_config(spec_for(purpose, "sarvam_router"), config),
        {"sarvam_api_key": SecretStr("s" * 20), other: SecretStr("o" * 20)},
    )


def test_every_launch_language_is_heard_and_spoken(settings: Settings) -> None:
    languages = sorted(LAUNCH_LANGUAGES)
    policy = LanguagePolicy(default_language="en", enabled_languages=languages, auto_detect=True)
    validate_voices(settings, policy, connection("stt"), connection("tts", model_id="eleven_v3"))


def test_connections_build_the_routers(settings: Settings) -> None:
    policy = LanguagePolicy(default_language="hi", enabled_languages=["hi", "es"])
    stt = stt_from_connection(settings, connection("stt"), None, policy)
    tts = tts_from_connection(settings, connection("tts"), "IN")
    assert isinstance(stt, SarvamRouterSTTProvider) and stt.route()[0] == "sarvam"
    assert isinstance(tts, SarvamRouterTTSProvider) and tts.country == "IN"


def test_platform_readiness_needs_both_keys(settings: Settings) -> None:
    both = {"sarvam_api_key": SecretStr("s" * 20), "deepgram_api_key": SecretStr("d" * 20)}
    ready = settings.model_copy(
        update={"stt_provider": "sarvam_router", "routed_stt_secrets": both}
    )
    assert _stt_key(ready)
    half = ready.model_copy(update={"routed_stt_secrets": {"sarvam_api_key": SecretStr("s" * 20)}})
    assert not _stt_key(half)
    sarvam = settings.model_copy(
        update={"tts_provider": "sarvam", "sarvam_tts_key": SecretStr("s" * 20)}
    )
    assert _tts_key(sarvam)


def test_support_staff_can_open_a_clinics_numbers() -> None:
    assert ("GET", "/api/v1/tenants/current/phone-numbers") in SUPPORT_ROUTES
    assert ("PATCH", "/api/v1/tenants/current/phone-numbers/{number_id}") in SUPPORT_ROUTES
