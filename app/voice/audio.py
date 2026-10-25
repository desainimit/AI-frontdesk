from collections.abc import AsyncIterator
from typing import Any

from app.voice.codec import pcm_to_ulaw
from app.voice.tts.base import TextToSpeechProvider, TTSPermanentError

# 20 ms of telephone audio: 320 bytes as PCM16, 160 bytes as µ-law.
PCM_FRAME = 320
ULAW_FRAME = 160


async def pcm_frames(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Normalize arbitrary provider chunks into 20ms PCM16/8kHz frames."""
    pending = bytearray()
    async for chunk in source:
        if len(chunk) > 65536:
            raise TTSPermanentError("Speech audio chunk exceeds limit")
        pending.extend(chunk)
        while len(pending) >= PCM_FRAME:
            yield bytes(pending[:PCM_FRAME])
            del pending[:PCM_FRAME]
    if len(pending) % 2:
        raise TTSPermanentError("Speech provider returned a partial PCM sample")
    if pending:
        yield bytes(pending)


async def ulaw_speech(
    tts: TextToSpeechProvider, text: str, voice_id: str | None, language: str | None
) -> AsyncIterator[bytes]:
    """The provider's speech as 20ms µ-law frames, the format Twilio plays."""
    kwargs: dict[str, Any] = {} if language is None else {"language": language}
    async for frame in pcm_frames(tts.synthesize(text, voice_id, **kwargs)):
        yield pcm_to_ulaw(frame)
