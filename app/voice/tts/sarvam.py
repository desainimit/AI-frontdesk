"""Sarvam voices (Bulbul) for Indian languages and Indian English.

One REST request per sentence, asked for 8 kHz audio so it goes to the call as it is.
"""

import base64
import io
import wave
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
from pydantic import SecretStr

from app.core.settings import SpeakingSpeed
from app.languages import known
from app.languages import language as registry_language
from app.voice.http import retry_after_seconds
from app.voice.tts.base import (
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)

URL = "https://api.sarvam.ai/text-to-speech"
# Bulbul's pace for the platform's speaking speed (1.0 is its own normal pace).
PACE: dict[str, float] = {"slow": 0.8, "normal": 0.9, "fast": 1.0, "very_fast": 1.1}
MAX_CHARACTERS = 2500


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
        model: str = "bulbul:v3",
        speaker: str = "priya",
        speed: SpeakingSpeed = "normal",
        timeout_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self.model = model
        self.speaker = speaker
        self.speed = speed
        self._timeout = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return "sarvam"

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        key = self._key.get_secret_value()
        if not key:
            raise TTSPermanentError("Sarvam API key is not configured")
        if not text.strip():
            return
        code = registry_language(language).sarvam if language and known(language) else "en-IN"
        if code is None:
            raise TTSPermanentError("Sarvam has no voice for this language")
        body = {
            "text": text[:MAX_CHARACTERS],
            "language_code": code,
            "speaker": voice_id or self.speaker,
            "model": self.model,
            "pace": PACE.get(self.speed, 0.9),
            "speech_sample_rate": 8000,
            "output_audio_codec": "wav",
        }
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(
                URL, json=body, headers={"api-subscription-key": key}, timeout=self._timeout
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

    async def health(self) -> bool:
        return bool(self._key.get_secret_value())
