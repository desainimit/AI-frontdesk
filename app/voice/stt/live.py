import asyncio
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, Protocol, cast

from websockets.exceptions import ConnectionClosed

from app.voice.stt.base import STTPermanentError, STTTransientError, TranscriptionResult
from app.voice.stt.languages import language_counts


class SpeechSocket(Protocol):
    async def send(self, message: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class DeepgramLiveStream:
    """One live connection; final segments accumulate until a real utterance boundary."""

    def __init__(self, socket: SpeechSocket, language: str, timeout_seconds: float) -> None:
        self._socket = socket
        self._language = language
        self._timeout = timeout_seconds
        self._closed = False
        self._parts: list[str] = []
        self._language_counts: Counter[str] = Counter()
        self._keepalive = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(3)
                async with asyncio.timeout(self._timeout):
                    await self._socket.send('{"type":"KeepAlive"}')
        except (ConnectionClosed, OSError, TimeoutError):
            await self._socket.close()

    async def send_audio(self, audio: bytes) -> None:
        if self._closed:
            raise STTTransientError("Speech stream is closed")
        if len(audio) % 2:
            raise STTPermanentError("PCM16 requires complete samples")
        try:
            async with asyncio.timeout(self._timeout):
                await self._socket.send(audio)
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
                if kind == "UtteranceEnd":
                    if self._parts:
                        final = " ".join(self._parts)
                        languages = tuple(code for code, _ in self._language_counts.most_common())
                        self._parts.clear()
                        self._language_counts.clear()
                        yield TranscriptionResult(
                            final,
                            True,
                            language=languages[0] if languages else None,
                            languages=languages,
                        )
                    continue
                if kind != "Results":
                    continue
                channel = event["channel"]
                alternative = channel["alternatives"][0]
                counts = language_counts(channel, alternative)
                languages = tuple(code for code, _ in counts.most_common())
                language = languages[0] if languages else None
                text = alternative["transcript"]
                if not isinstance(text, str) or len(text) > 4000:
                    raise ValueError("Invalid transcript")
                confidence = float(alternative.get("confidence", 0))
                if event.get("is_final") is True and text.strip():
                    self._parts.append(text.strip())
                    self._language_counts.update(counts)
                if sum(map(len, self._parts)) > 4000:
                    raise STTTransientError("Utterance exceeds limit")
                if event.get("speech_final") is True:
                    final = " ".join(self._parts)
                    self._parts.clear()
                    final_languages = tuple(code for code, _ in self._language_counts.most_common())
                    final_language = final_languages[0] if final_languages else None
                    self._language_counts.clear()
                    if final:
                        yield TranscriptionResult(
                            final, True, confidence, final_language, languages=final_languages
                        )
                elif text.strip():
                    yield TranscriptionResult(
                        text, False, confidence, language, languages=languages
                    )
        except (ConnectionClosed, OSError):
            if not self._closed:
                raise STTTransientError("Live speech connection ended") from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise STTTransientError("Invalid live speech response") from None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._keepalive.cancel()
        with suppress(asyncio.CancelledError):
            await self._keepalive
        await self._socket.close()
