"""One ElevenLabs WebSocket per call, so a sentence starts as soon as it is written.

Each sentence gets its own context on the shared socket: the text goes out the moment
the model finishes it, audio comes back tagged with that context, and a sentence the
caller talks over is closed without waiting for the rest. Audio is µ-law 8 kHz, the
format Twilio plays, so nothing is converted on the way.

ElevenLabs sends a context's final message only once the context is closed, so each
sentence is flushed and closed straight away; closing a flushed context still finishes
its audio. (Waiting for that message without closing is what timed calls out.)
"""

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import suppress
from typing import Any, Protocol, cast
from urllib.parse import quote, urlencode
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from app.voice.tts.base import (
    TTSError,
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)

# ElevenLabs allows five contexts per connection; one is kept free for the next reply.
MAX_CONTEXTS = 4
# Long enough for a caller who talks for a while between two replies.
INACTIVITY_SECONDS = 180
# Audio has arrived and then none for this long: the sentence is over, final message or not.
QUIET_AFTER_AUDIO_SECONDS = 1.0


class SpeechSocket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class ElevenLabsSocket:
    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model_id: str,
        language_code: str | None,
        voice_settings: Mapping[str, float],
        base_url: str = "https://api.elevenlabs.io",
        timeout_seconds: float = 5.0,
        connector: Any = connect,
    ) -> None:
        params: dict[str, str | int] = {
            "model_id": model_id,
            "output_format": "ulaw_8000",
            "inactivity_timeout": INACTIVITY_SECONDS,
            # Whole sentences are sent, so the generation buffers only add delay.
            "auto_mode": "true",
        }
        if language_code:
            params["language_code"] = language_code
        origin = (
            base_url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        )
        self._url = (
            f"{origin}/v1/text-to-speech/{quote(voice_id, safe='')}/multi-stream-input?"
            + urlencode(params)
        )
        self._api_key = api_key
        self._voice_settings = dict(voice_settings)
        self._timeout = timeout_seconds
        self._connector = connector
        self._socket: SpeechSocket | None = None
        self._reader: asyncio.Task[None] | None = None
        self._contexts: dict[str, asyncio.Queue[bytes | TTSError | None]] = {}
        self._slots = asyncio.Semaphore(MAX_CONTEXTS)
        self._connecting = asyncio.Lock()
        self._sending = asyncio.Lock()
        self._closed = False

    async def connect(self) -> None:
        """Open the socket ahead of the first sentence; later calls reuse it."""
        async with self._connecting:
            if self._closed:
                raise TTSTransientError("Speech connection is closed")
            if self._socket is not None and self._reader is not None and not self._reader.done():
                return
            try:
                socket = await self._connector(
                    self._url,
                    additional_headers={"xi-api-key": self._api_key},
                    open_timeout=self._timeout,
                    close_timeout=2,
                    max_size=1 << 20,
                )
            except InvalidStatus as error:
                status = error.response.status_code
                if status in {401, 403}:
                    raise TTSPermanentError("ElevenLabs authentication failed") from None
                if status == 429:
                    raise TTSRateLimitError(retry_after=5) from None
                raise TTSTransientError("ElevenLabs handshake failed") from None
            except (WebSocketException, OSError, TimeoutError):
                raise TTSTransientError("ElevenLabs connection failed") from None
            self._socket = cast(SpeechSocket, socket)
            self._reader = asyncio.create_task(self._read(self._socket))

    async def speak(self, text: str) -> AsyncGenerator[bytes]:
        """The audio of one sentence, as it arrives."""
        if not text.strip():
            return
        async with self._slots:
            await self.connect()
            context = uuid4().hex
            queue: asyncio.Queue[bytes | TTSError | None] = asyncio.Queue()
            self._contexts[context] = queue
            closed = False
            heard = False
            try:
                await self._send(
                    {"text": " ", "context_id": context, "voice_settings": self._voice_settings}
                )
                await self._send({"text": text.strip() + " ", "context_id": context, "flush": True})
                await self._send({"context_id": context, "close_context": True})
                closed = True
                while True:
                    wait = QUIET_AFTER_AUDIO_SECONDS if heard else self._timeout
                    try:
                        async with asyncio.timeout(wait):
                            item = await queue.get()
                    except TimeoutError:
                        if heard:
                            return
                        raise TTSTimeoutError("ElevenLabs synthesis timed out") from None
                    if item is None:
                        return
                    if isinstance(item, TTSError):
                        raise item
                    heard = True
                    yield item
            finally:
                self._contexts.pop(context, None)
                if not closed:
                    # It failed before it was closed: stop generating it.
                    with suppress(TTSError):
                        await self._send({"context_id": context, "close_context": True})

    async def _send(self, message: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise TTSTransientError("Speech connection is not open")
        try:
            async with self._sending, asyncio.timeout(self._timeout):
                await socket.send(json.dumps(message))
        except (ConnectionClosed, OSError, TimeoutError):
            raise TTSTransientError("ElevenLabs send failed") from None

    async def _read(self, socket: SpeechSocket) -> None:
        failure: TTSError = TTSTransientError("ElevenLabs connection ended")
        try:
            while True:
                raw = await socket.recv()
                if not isinstance(raw, str):
                    continue
                parsed: object = json.loads(raw)
                if not isinstance(parsed, dict):
                    continue
                event = cast(dict[str, Any], parsed)
                queue = self._contexts.get(str(event.get("contextId") or event.get("context_id")))
                if event.get("error") or (event.get("message") and "audio" not in event):
                    failure = _failure(event)
                    if queue is not None:
                        queue.put_nowait(failure)
                        continue
                    if "context" in str(event.get("error") or event.get("message")).lower():
                        # About a context already finished (a sentence the caller talked
                        # over, say): the connection itself is fine.
                        continue
                    break
                if queue is None:
                    continue
                audio = event.get("audio")
                if isinstance(audio, str) and audio:
                    queue.put_nowait(base64.b64decode(audio, validate=True))
                if event.get("isFinal") is True or event.get("is_final") is True:
                    queue.put_nowait(None)
        except (ConnectionClosed, OSError):
            pass
        except (ValueError, binascii.Error):
            failure = TTSTransientError("Invalid ElevenLabs response")
        finally:
            # Whoever is still waiting learns the connection is gone; the next sentence
            # opens a new one.
            for queue in self._contexts.values():
                queue.put_nowait(failure)
            if self._socket is socket:
                self._socket = None
            with suppress(Exception):
                await socket.close()

    async def close(self) -> None:
        self._closed = True
        socket = self._socket
        if socket is not None:
            with suppress(TTSError):
                await self._send({"close_socket": True})
            with suppress(Exception):
                await socket.close()
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._reader


def _failure(event: dict[str, Any]) -> TTSError:
    text = str(event.get("error") or event.get("message") or "").lower()
    if "quota" in text or "rate" in text or "concurrent" in text:
        return TTSRateLimitError(retry_after=5)
    if "auth" in text or "api key" in text or "unauthorized" in text:
        return TTSPermanentError("ElevenLabs authentication failed")
    return TTSTransientError("ElevenLabs reported an error")
