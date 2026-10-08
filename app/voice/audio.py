from collections.abc import AsyncIterator

from app.voice.tts.base import TTSPermanentError


async def pcm_frames(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Normalize arbitrary provider chunks into 20ms PCM16/8kHz frames."""
    pending = bytearray()
    async for chunk in source:
        if len(chunk) > 65536:
            raise TTSPermanentError("Speech audio chunk exceeds limit")
        pending.extend(chunk)
        while len(pending) >= 320:
            yield bytes(pending[:320])
            del pending[:320]
    if len(pending) % 2:
        raise TTSPermanentError("Speech provider returned a partial PCM sample")
    if pending:
        yield bytes(pending)
