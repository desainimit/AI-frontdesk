"""Deepgram Flux: recognition that also decides when the caller has finished.

Flux listens for the end of a thought, not for a fixed silence, so a caller who
pauses mid-sentence is not answered too early and one who has finished is answered
without waiting out a timer. Its start-of-turn event is also the earliest sign that
the caller has begun talking over a reply.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, cast

from websockets.exceptions import ConnectionClosed

from app.voice.stt.base import STTPermanentError, STTTransientError, TranscriptionResult
from app.voice.stt.live import SpeechSocket

# Flux works best with about 80 ms of audio per message; Twilio sends 20 ms frames.
BATCH_BYTES = 1280


def _confidence(words: object) -> float:
    if not isinstance(words, list) or not words:
        return 1.0
    values: list[float] = []
    for word in cast(list[object], words):
        if isinstance(word, dict):
            with suppress(TypeError, ValueError):
                values.append(float(cast(dict[str, Any], word).get("confidence", 1.0)))
    if not values:
        return 1.0
    return min(1.0, max(0.0, sum(values) / len(values)))


def _languages(event: dict[str, Any]) -> tuple[str, ...]:
    raw = event.get("languages")
    if not isinstance(raw, list):
        return ()
    return tuple(str(code).split("-")[0].lower() for code in cast(list[object], raw) if code)[:4]


class FluxLiveStream:
    def __init__(self, socket: SpeechSocket, timeout_seconds: float) -> None:
        self._socket = socket
        self._timeout = timeout_seconds
        self._closed = False
        self._pending = bytearray()

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise STTTransientError("Speech stream is closed")
        if len(audio) % 2:
            raise STTPermanentError("PCM16 requires complete samples")
        self._pending.extend(audio)
        if len(self._pending) < BATCH_BYTES:
            return
        chunk, self._pending = bytes(self._pending), bytearray()
        try:
            async with asyncio.timeout(self._timeout):
                await self._socket.send(chunk)
        except (ConnectionClosed, OSError, TimeoutError):
            raise STTTransientError("Live speech send failed") from None

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        try:
            while not self._closed:
                raw = await self._socket.recv()
                if not isinstance(raw, str) or len(raw) > 65536:
                    raise STTTransientError("Invalid speech response")
                parsed: object = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("Invalid speech event")
                event = cast(dict[str, Any], parsed)
                kind = event.get("type")
                if kind == "Error":
                    raise STTTransientError("Speech provider reported an error")
                if kind != "TurnInfo":
                    continue
                text = event.get("transcript") or ""
                if not isinstance(text, str) or len(text) > 4000:
                    raise ValueError("Invalid transcript")
                languages = _languages(event)
                language = languages[0] if languages else None
                confidence = _confidence(event.get("words"))
                name = event.get("event")
                if name == "EndOfTurn":
                    if text.strip():
                        yield TranscriptionResult(
                            text.strip(), True, confidence, language, languages=languages
                        )
                elif name == "StartOfTurn":
                    yield TranscriptionResult(
                        text, False, confidence, language, languages=languages, speech_started=True
                    )
                elif name in {"Update", "TurnResumed"} and text.strip():
                    yield TranscriptionResult(
                        text, False, confidence, language, languages=languages
                    )
        except (ConnectionClosed, OSError):
            if not self._closed:
                raise STTTransientError("Live speech connection ended") from None
        except (ValueError, KeyError, TypeError):
            raise STTTransientError("Invalid live speech response") from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(ConnectionClosed, OSError, TimeoutError):
            async with asyncio.timeout(2):
                await self._socket.send('{"type":"CloseStream"}')
        await self._socket.close()
