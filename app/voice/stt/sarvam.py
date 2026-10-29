"""Sarvam speech recognition (Saaras), built for Indian languages and Indian English.

Live: the realtime WebSocket, fed the call's 8 kHz audio as it comes. Short audio: the
REST endpoint. Sarvam keeps request data for as long as its workspace's retention
setting says; set it to the shortest period in the Sarvam dashboard.
"""

import asyncio
import base64
import io
import json
import wave
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, cast
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from app.languages import known
from app.languages import language as registry_language
from app.voice.http import retry_after_seconds
from app.voice.stt.base import (
    STTPermanentError,
    STTRateLimitError,
    STTStream,
    STTTimeoutError,
    STTTransientError,
    TranscriptionResult,
)

REALTIME_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
REST_URL = "https://api.sarvam.ai/speech-to-text"
# Closed by Sarvam: rate limit, quota or key (1003); bad parameters or account (4000).
_REFUSED = {1003, 4000}
PING_SECONDS = 20


def sarvam_code(language: str | None) -> str:
    """Sarvam's code for a call language, or "auto" for one it does not name."""
    if language and known(language):
        return registry_language(language).sarvam or "auto"
    return "auto"


def _language(code: object) -> str | None:
    """ "hi-IN" -> "hi"; Sarvam names languages with the region."""
    return str(code).split("-")[0].lower() if isinstance(code, str) and code else None


class SarvamLiveStream:
    def __init__(self, socket: Any, timeout_seconds: float) -> None:
        self._socket = socket
        self._timeout = timeout_seconds
        self._closed = False
        self._keepalive = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
        # The connection closes after a minute without traffic (1008).
        with suppress(ConnectionClosed, OSError, TimeoutError, asyncio.CancelledError):
            while not self._closed:
                await asyncio.sleep(PING_SECONDS)
                async with asyncio.timeout(self._timeout):
                    await self._socket.send('{"event": "ping"}')

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise STTTransientError("Speech stream is closed")
        if len(audio) % 2:
            raise STTPermanentError("PCM16 requires complete samples")
        message = json.dumps({"event": "audio_input", "audio": base64.b64encode(audio).decode()})
        try:
            async with asyncio.timeout(self._timeout):
                await self._socket.send(message)
        except (ConnectionClosed, OSError, TimeoutError):
            raise STTTransientError("Live speech send failed") from None

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        try:
            while not self._closed:
                raw = await self._socket.recv()
                if not isinstance(raw, str) or len(raw) > 65536:
                    raise STTTransientError("Invalid speech response")
                event = cast(dict[str, Any], json.loads(raw))
                kind = event.get("event")
                if kind == "error":
                    if event.get("is_fatal"):
                        raise STTTransientError("Speech recognition stopped")
                    continue
                if kind == "vad.speech_start":
                    yield TranscriptionResult("", False, speech_started=True)
                elif kind in {"transcript.partial", "transcript.final"}:
                    text = str(event.get("text") or "").strip()[:4000]
                    if not text:
                        continue
                    heard = _language(event.get("language"))
                    yield TranscriptionResult(
                        text,
                        kind == "transcript.final",
                        float(event.get("language_confidence") or 1.0)
                        if kind == "transcript.final"
                        else 1.0,
                        heard,
                        languages=(heard,) if heard else (),
                    )
                elif kind == "session.end":
                    return
        except ConnectionClosed as error:
            if error.rcvd is not None and error.rcvd.code in _REFUSED:
                raise STTPermanentError("Sarvam refused the speech stream") from None
            if not self._closed:
                raise STTTransientError("Speech stream closed") from None
        except ValueError:
            raise STTTransientError("Invalid speech response") from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._keepalive.cancel()
        with suppress(ConnectionClosed, OSError, TimeoutError):
            async with asyncio.timeout(self._timeout):
                await self._socket.send('{"event": "end"}')
        with suppress(Exception):
            await self._socket.close()


class SarvamSTTProvider:
    def __init__(
        self,
        api_key: SecretStr,
        model: str = "saaras:v3-realtime",
        language: str | None = None,
        *,
        stream_type: str = "fast",
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self.model = model
        # The call's language, or None to let Sarvam tell which Indian language it is.
        self.language = language
        self.stream_type = stream_type
        self._timeout = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return "sarvam"

    def _headers(self) -> dict[str, str]:
        key = self._key.get_secret_value()
        if not key:
            raise STTPermanentError("Sarvam API key is not configured")
        return {"api-subscription-key": key}

    def _query(self, sample_rate: int, encoding: str) -> str:
        return urlencode(
            {
                "language_code": sarvam_code(self.language),
                "model": self.model,
                "stream_type": self.stream_type,
                # Hinglish comes back as people say it: Hindi words with English ones.
                "mode": "codemix" if self.language == "hi-Latn" else "transcribe",
                "encoding": "mulaw" if encoding == "mulaw" else "linear16",
                "sample_rate": 8000 if sample_rate <= 8000 else 16000,
            }
        )

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        url = f"{REALTIME_URL}?{self._query(sample_rate, encoding)}"
        try:
            async with asyncio.timeout(self._timeout):
                socket = await connect(url, additional_headers=self._headers())
        except InvalidStatus as error:
            status = error.response.status_code
            if status == 429:
                raise STTRateLimitError() from None
            if status in {401, 403}:
                raise STTPermanentError("Sarvam rejected the API key") from None
            raise STTTransientError(f"Sarvam returned {status}") from None
        except TimeoutError:
            raise STTTimeoutError("Sarvam did not answer in time") from None
        except (WebSocketException, OSError):
            raise STTTransientError("Sarvam could not be reached") from None
        return SarvamLiveStream(socket, self._timeout)

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(sample_rate)
            file.writeframes(audio)
        data = {"model": "saaras:v3", "language_code": sarvam_code(self.language)}
        if self.language == "hi-Latn":
            data["mode"] = "codemix"
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                REST_URL,
                headers=self._headers(),
                data=data,
                files={"file": ("audio.wav", buffer.getvalue(), "audio/wav")},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise STTTimeoutError("Sarvam did not answer in time") from None
        except httpx.RequestError:
            raise STTTransientError("Sarvam could not be reached") from None
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code == 429:
            raise STTRateLimitError(
                retry_after=retry_after_seconds(response.headers.get("retry-after"))
            )
        if response.status_code in {401, 403}:
            raise STTPermanentError("Sarvam rejected the API key")
        if response.status_code >= 500:
            raise STTTransientError(f"Sarvam returned {response.status_code}")
        if response.status_code >= 400:
            raise STTPermanentError(f"Sarvam rejected the request with {response.status_code}")
        try:
            body = cast(dict[str, Any], response.json())
        except ValueError:
            raise STTTransientError("Invalid speech response") from None
        heard = _language(body.get("language_code"))
        return TranscriptionResult(
            str(body.get("transcript") or "").strip()[:4000],
            True,
            1.0,
            heard,
            languages=(heard,) if heard else (),
        )

    async def health(self) -> bool:
        return bool(self._key.get_secret_value())
