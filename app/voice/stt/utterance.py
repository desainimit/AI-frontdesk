import asyncio
import audioop
from collections.abc import AsyncIterator
from contextlib import suppress

from app.voice.stt.base import (
    SpeechToTextProvider,
    STTError,
    STTPermanentError,
    STTTransientError,
    TranscriptionResult,
)


class UtteranceSTTStream:
    """Bounded endpointed REST fallback, with one request at a time.

    A 600ms silence or ten-second cap ends an utterance during the call.
    This adapter doesn't claim to provide server-side interim recognition.
    """

    def __init__(self, provider: SpeechToTextProvider, sample_rate: int, encoding: str) -> None:
        if encoding != "linear16" or sample_rate not in {8000, 16000}:
            raise STTPermanentError("Unsupported speech input format")
        self._provider = provider
        self._sample_rate = sample_rate
        self._frame_bytes = sample_rate // 50 * 2
        self._pending = bytearray()
        self._buffer = bytearray()
        self._preroll = bytearray()
        self._silence = 0
        self._closed = False
        self._error: STTError | None = None
        self._requests: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._results: asyncio.Queue[TranscriptionResult | STTError | None] = asyncio.Queue(
            maxsize=8
        )
        self._worker = asyncio.create_task(self._recognize())

    async def send_audio(self, audio: bytes) -> None:
        if self._error:
            raise self._error
        if self._closed:
            raise STTTransientError("Speech stream is closed")
        if len(audio) % 2:
            raise STTPermanentError("PCM16 requires complete samples")
        self._pending.extend(audio)
        while len(self._pending) >= self._frame_bytes:
            frame = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            voiced = audioop.rms(frame, 2) >= 200
            if not self._buffer and not voiced:
                self._preroll.extend(frame)
                del self._preroll[: -self._frame_bytes * 5]
                continue
            if not self._buffer:
                self._buffer.extend(self._preroll)
                self._preroll.clear()
            self._buffer.extend(frame)
            self._silence = 0 if voiced else self._silence + 1
            if self._silence >= 30 or len(self._buffer) >= self._sample_rate * 2 * 10:
                try:
                    self._requests.put_nowait(bytes(self._buffer))
                except asyncio.QueueFull:
                    raise STTTransientError("Speech recognition backlog exceeded") from None
                self._buffer.clear()
                self._silence = 0

    async def _recognize(self) -> None:
        while not self._closed:
            audio = await self._requests.get()
            try:
                result = await self._provider.transcribe(audio, self._sample_rate)
                if result.text.strip():
                    await self._results.put(result)
            except STTError as error:
                self._error = error
                await self._results.put(error)
                return
            except Exception:
                self._error = STTTransientError("Speech recognition failed")
                await self._results.put(self._error)
                return
            finally:
                self._requests.task_done()

    async def results(self) -> AsyncIterator[TranscriptionResult]:
        while True:
            item = await self._results.get()
            if item is None:
                return
            if isinstance(item, STTError):
                raise item
            yield item

    async def close(self) -> None:
        self._closed = True
        self._worker.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker
        self._buffer.clear()
        self._pending.clear()
        self._preroll.clear()
        while not self._requests.empty():
            self._requests.get_nowait()
            self._requests.task_done()
        while not self._results.empty():
            self._results.get_nowait()
        self._results.put_nowait(None)
