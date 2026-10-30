"""Sarvam voices (Bulbul) for Indian languages and Indian English.

Streamed: replies go over Sarvam's text-to-speech WebSocket, asked for 8 kHz PCM, and
each piece of audio goes to the call as soon as it arrives instead of after the whole
sentence is made. A socket is kept open between sentences of one call. If the stream
cannot be used, the REST endpoint says the sentence instead, and for the rest of the
call. Sarvam keeps request data for as long as its workspace's retention setting says;
set it to the shortest period in the Sarvam dashboard.
"""

import asyncio
import base64
import io
import json
import logging
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from time import monotonic
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from app.core.settings import SpeakingSpeed
from app.languages import family, known
from app.languages import language as registry_language
from app.voice.http import retry_after_seconds
from app.voice.tts.base import (
    TTSError,
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)

logger = logging.getLogger(__name__)

URL = "https://api.sarvam.ai/text-to-speech"
SOCKET_URL = "wss://api.sarvam.ai/text-to-speech/ws"
# Bulbul's pace for the platform's speaking speed (1.0 is its own normal pace).
PACE: dict[str, float] = {"slow": 0.8, "normal": 0.9, "fast": 1.0, "very_fast": 1.1}
PACE_V4: dict[str, float] = {"slow": 0.85, "normal": 0.95, "fast": 1.05, "very_fast": 1.15}
V4_MODEL = "bulbul:v4-flash"
# Bulbul v4 Flash's voices, used when a connection names none.
V4_VOICES: dict[str, str] = {
    "hi": "ishita_enhi_customer",
    "gu": "pooja_gu_customer",
    "en": "ishita_en_customer",
}
MAX_CHARACTERS = 2500
# Sarvam closes a socket after a minute without traffic; one idle longer than this is
# not reused.
IDLE_SECONDS = 45.0
# Sarvam starts on a sentence once this many characters are waiting (its least); every
# sentence is flushed as well, so a short one is not held back.
MIN_BUFFER = 30

Connect = Callable[..., Awaitable[Any]]


def _pcm(wav_bytes: bytes) -> bytes:
    """The samples of a WAV file; raw PCM is returned as it is."""
    if not wav_bytes.startswith(b"RIFF"):
        return wav_bytes
    with wave.open(io.BytesIO(wav_bytes), "rb") as file:
        if file.getnchannels() != 1 or file.getsampwidth() != 2 or file.getframerate() != 8000:
            raise TTSTransientError("Sarvam returned audio in another format")
        return file.readframes(file.getnframes())


class SarvamTTSProvider:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str = V4_MODEL,
        speaker: str = "priya",
        voices: Mapping[str, str] | None = None,
        speed: SpeakingSpeed = "normal",
        timeout_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
        streaming: bool = True,
        connector: Connect | None = None,
    ) -> None:
        self._key = api_key
        self.model = model
        self.speaker = speaker
        # Bulbul v4 Flash's voices are one language each; v3's one voice speaks them all.
        self.voices = dict(voices or V4_VOICES) if model == V4_MODEL else {}
        self.speed = speed
        self._timeout = timeout_seconds
        self._client = client
        # Off for the rest of the call once the stream failed before any audio.
        self.streaming = streaming
        self._connect: Connect = connector or connect
        # Open sockets not in use, with when each was last used.
        self._idle: list[tuple[Any, float]] = []

    @property
    def name(self) -> str:
        return "sarvam"

    @property
    def voice_key(self) -> str:
        voices = ",".join(self.voices[code] for code in sorted(self.voices)) or self.speaker
        return f"sarvam:{self.model}:{voices}:{self._pace}"

    @property
    def _pace(self) -> float:
        pace = PACE_V4 if self.model == V4_MODEL else PACE
        return pace.get(self.speed, pace["normal"])

    def _speaker(self, language: str | None) -> str:
        if not self.voices:
            return self.speaker
        spoken = family(language) if language and known(language) else "en"
        return self.voices.get(spoken, self.voices.get("en", self.speaker))

    def _language_code(self, language: str | None) -> str:
        code = registry_language(language).sarvam if language and known(language) else "en-IN"
        if code is None:
            raise TTSPermanentError("Sarvam has no voice for this language")
        return code

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        if not self._key.get_secret_value():
            raise TTSPermanentError("Sarvam API key is not configured")
        if not text.strip():
            return
        code = self._language_code(language)
        speaker = voice_id or self._speaker(language)
        # Bulbul reads "!" with force; a receptionist does not raise her voice.
        text = text.replace("!", ".")
        if self.streaming:
            started = False
            try:
                async for chunk in self._stream(text[:MAX_CHARACTERS], code, speaker):
                    started = True
                    yield chunk
                return
            except TTSError as error:
                # Audio already played cannot be taken back; otherwise REST says it.
                if started or isinstance(error, TTSPermanentError):
                    raise
                self.streaming = False
                logger.warning("sarvam_voice_stream_unavailable", extra={"reason": error.code})
        async for chunk in self._rest(text, code, speaker):
            yield chunk

    async def _open(self) -> Any:
        query = urlencode({"model": self.model, "send_completion_event": "true"})
        try:
            async with asyncio.timeout(self._timeout):
                return await self._connect(
                    f"{SOCKET_URL}?{query}",
                    additional_headers={"api-subscription-key": self._key.get_secret_value()},
                )
        except InvalidStatus as error:
            status = error.response.status_code
            if status == 429:
                raise TTSRateLimitError() from None
            if status in {401, 403}:
                raise TTSPermanentError("Sarvam rejected the API key") from None
            raise TTSTransientError(f"Sarvam returned {status}") from None
        except TimeoutError:
            raise TTSTimeoutError("Sarvam voice did not answer in time") from None
        except (WebSocketException, OSError):
            raise TTSTransientError("Sarvam could not be reached") from None

    async def _socket(self) -> tuple[Any, bool]:
        """A socket for one sentence, and whether it was used before."""
        while self._idle:
            socket, used = self._idle.pop()
            if monotonic() - used < IDLE_SECONDS:
                return socket, True
            await _close(socket)
        return await self._open(), False

    def _messages(self, text: str, code: str, speaker: str) -> list[str]:
        config = {
            "language_code": code,
            # The name Sarvam's earlier releases read; both say the same.
            "target_language_code": code,
            "speaker": speaker,
            "model": self.model,
            "pace": self._pace,
            "speech_sample_rate": 8000,
            "output_audio_codec": "linear16",
            "min_buffer_size": MIN_BUFFER,
        }
        return [
            json.dumps({"type": "config", "data": config}),
            json.dumps({"type": "text", "data": {"text": text}}),
            json.dumps({"type": "flush"}),
        ]

    async def _stream(self, text: str, code: str, speaker: str) -> AsyncIterator[bytes]:
        socket, reused = await self._socket()
        finished = False
        try:
            try:
                for message in self._messages(text, code, speaker):
                    await socket.send(message)
            except (ConnectionClosed, OSError):
                if not reused:
                    raise TTSTransientError("Sarvam voice stream closed") from None
                # Closed by Sarvam while it waited: one fresh socket, once.
                await _close(socket)
                socket = await self._open()
                for message in self._messages(text, code, speaker):
                    await socket.send(message)
            odd = b""
            while True:
                try:
                    async with asyncio.timeout(self._timeout):
                        raw = await socket.recv()
                except TimeoutError:
                    raise TTSTimeoutError("Sarvam voice timed out") from None
                except (ConnectionClosed, OSError):
                    raise TTSTransientError("Sarvam voice stream closed") from None
                message = _parse(raw)
                kind = message.get("type")
                data = cast(dict[str, Any], message.get("data") or {})
                if kind == "audio":
                    try:
                        audio = odd + _pcm(base64.b64decode(str(data.get("audio") or "")))
                    except (ValueError, wave.Error):
                        raise TTSTransientError("Sarvam returned no usable audio") from None
                    # Samples are two bytes; one split across pieces waits for the next.
                    whole = len(audio) - len(audio) % 2
                    audio, odd = audio[:whole], audio[whole:]
                    if audio:
                        yield audio
                elif kind == "event" and data.get("event_type") == "final":
                    finished = True
                    return
                elif kind == "error":
                    code_number = data.get("code")
                    if code_number in {401, 403}:
                        raise TTSPermanentError("Sarvam rejected the API key")
                    if code_number == 429:
                        raise TTSRateLimitError()
                    logger.warning("sarvam_voice_refused", extra={"code": code_number})
                    raise TTSTransientError("Sarvam voice stream refused the request")
        finally:
            if finished:
                self._idle.append((socket, monotonic()))
            else:
                # Stopped part way (the caller spoke, or an error): its leftover audio
                # must not reach the next sentence.
                await _close(socket)

    async def _rest(self, text: str, code: str, speaker: str) -> AsyncIterator[bytes]:
        body = {
            "text": text[:MAX_CHARACTERS],
            "language_code": code,
            "target_language_code": code,
            "speaker": speaker,
            "model": self.model,
            "pace": self._pace,
            "speech_sample_rate": 8000,
            "output_audio_codec": "wav",
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                URL,
                json=body,
                headers={"api-subscription-key": self._key.get_secret_value()},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as error:
            raise TTSTimeoutError("Sarvam voice timed out") from error
        except httpx.RequestError as error:
            raise TTSTransientError("Sarvam could not be reached") from error
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code == 429:
            raise TTSRateLimitError(
                retry_after=retry_after_seconds(response.headers.get("retry-after"))
            )
        if response.status_code in {401, 403}:
            raise TTSPermanentError("Sarvam rejected the API key")
        if response.status_code >= 500:
            raise TTSTransientError(f"Sarvam returned {response.status_code}")
        if response.status_code >= 400:
            raise TTSPermanentError(f"Sarvam rejected the request with {response.status_code}")
        try:
            audios = cast(list[Any], cast(dict[str, Any], response.json()).get("audios") or [])
            audio = _pcm(base64.b64decode(str(audios[0]))) if audios else b""
        except (ValueError, wave.Error, IndexError):
            raise TTSTransientError("Sarvam returned no usable audio") from None
        if not audio:
            raise TTSTransientError("Sarvam returned no audio")
        for start in range(0, len(audio), 1600):
            yield audio[start : start + 1600]

    async def warm_up(self, voice_id: str | None = None, *, language: str | None = None) -> None:
        """Open the socket while the call starts, so the greeting does not wait for it."""
        if self.streaming and not self._idle and self._key.get_secret_value():
            with suppress(TTSError):
                self._idle.append((await self._open(), monotonic()))

    async def health(self) -> bool:
        return bool(self._key.get_secret_value())

    async def aclose(self) -> None:
        idle, self._idle = self._idle, []
        for socket, _ in idle:
            await _close(socket)


def _parse(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw) > 4_000_000:
        raise TTSTransientError("Invalid voice response")
    try:
        message = json.loads(raw)
    except ValueError:
        raise TTSTransientError("Invalid voice response") from None
    if not isinstance(message, dict):
        raise TTSTransientError("Invalid voice response")
    return cast(dict[str, Any], message)


async def _close(socket: Any) -> None:
    with suppress(Exception):
        await socket.close()
