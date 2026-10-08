from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.conversation.locales import LOCALES, normalize_language
from app.voice.http import retry_after_seconds
from app.voice.tts.base import (
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)


class ElevenLabsTTSProvider:
    """Production ElevenLabs streaming TTS provider."""

    def __init__(
        self,
        api_key: SecretStr,
        *,
        default_voice_id: str = "21m00Tcm4TlvDq8ikWAM",  # Rachel
        model_id: str = "eleven_turbo_v2_5",
        output_format: str = "pcm_8000",
        base_url: str = "https://api.elevenlabs.io",
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.default_voice_id = default_voice_id
        self.model_id = model_id
        self.output_format = output_format
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return "elevenlabs"

    def _get_client(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=self.timeout_seconds)

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        secret = self._api_key.get_secret_value()
        if not secret:
            raise TTSPermanentError("ElevenLabs API key is not configured")
        if not text.strip():
            return

        target_voice = quote(voice_id or self.default_voice_id, safe="")
        url = f"{self.base_url}/v1/text-to-speech/{target_voice}/stream"
        params = {"output_format": self.output_format}
        headers = {
            "xi-api-key": secret,
            "Content-Type": "application/json",
            "Accept": "audio/x-raw",
        }
        payload: dict[str, Any] = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.8,
            },
        }

        if language:
            code = normalize_language(language)
            if code in LOCALES:
                payload["language_code"] = code
            elif code != "hinglish":
                raise TTSPermanentError("Unsupported speech language")

        owns_client = self._client is None
        client = self._get_client()
        try:
            async with client.stream(
                "POST",
                url,
                params=params,
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            ) as response:
                if response.status_code == 429:
                    retry_after = retry_after_seconds(response.headers.get("retry-after"))
                    raise TTSRateLimitError(retry_after=retry_after)
                if response.status_code in (401, 403):
                    raise TTSPermanentError("ElevenLabs authentication failed")
                if response.status_code >= 500:
                    raise TTSTransientError(f"ElevenLabs returned status {response.status_code}")
                if response.status_code >= 400:
                    raise TTSPermanentError(
                        f"ElevenLabs rejected request with {response.status_code}"
                    )

                async for chunk in response.aiter_bytes(chunk_size=1024):
                    if chunk:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise TTSTimeoutError("ElevenLabs synthesis timed out") from exc
        except httpx.RequestError as exc:
            raise TTSTransientError("ElevenLabs connection error") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def health(self) -> bool:
        if not self._api_key.get_secret_value():
            return False
        owns_client = self._client is None
        client = self._get_client()
        try:
            resp = await client.get(
                f"{self.base_url}/v1/user",
                headers={"xi-api-key": self._api_key.get_secret_value()},
                timeout=3.0,
            )
            return resp.status_code == 200
        except httpx.RequestError:
            return False
        finally:
            if owns_client:
                await client.aclose()
