from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.conversation.locales import LOCALES, normalize_language
from app.voice.http import retry_after_seconds
from app.voice.tts.base import (
    TTSError,
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)

# A little less stable than the default sounds livelier and less read-out.
VOICE_SETTINGS = {"stability": 0.45, "similarity_boost": 0.75}


class ElevenLabsTTSProvider:
    """Production ElevenLabs streaming TTS provider.

    One instance serves one call: its HTTP client is opened once, reused for every
    sentence, and closed with the call.
    """

    def __init__(
        self,
        api_key: SecretStr,
        *,
        default_voice_id: str = "21m00Tcm4TlvDq8ikWAM",  # Rachel
        model_id: str = "eleven_flash_v2_5",
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
        self._owns_client = client is None

    @property
    def name(self) -> str:
        return "elevenlabs"

    @property
    def voice_key(self) -> str:
        """What decides how a phrase sounds, for reusing audio already made."""
        return f"elevenlabs:{self.model_id}:{self.default_voice_id}"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    def _secret(self) -> str:
        secret = self._api_key.get_secret_value()
        if not secret:
            raise TTSPermanentError("ElevenLabs API key is not configured")
        return secret

    def _language_code(self, language: str | None) -> str | None:
        if not language:
            return None
        code = normalize_language(language)
        if code == "gu" and self.model_id != "eleven_v3":
            raise TTSPermanentError("Gujarati requires ElevenLabs v3 or Azure Speech voice")
        if code in LOCALES:
            return code
        if code != "hinglish":
            raise TTSPermanentError("Unsupported speech language")
        return None

    async def warm_up(self, voice_id: str | None = None, *, language: str | None = None) -> None:
        """Open the connection before the first words are needed (a free request)."""
        with suppress(httpx.HTTPError, TTSError):
            await self._get_client().get(
                f"{self.base_url}/v1/models", headers={"xi-api-key": self._secret()}, timeout=3.0
            )

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        async for chunk in self._http(text, voice_id, language, self.output_format):
            yield chunk

    async def _http(
        self, text: str, voice_id: str | None, language: str | None, output_format: str
    ) -> AsyncIterator[bytes]:
        secret = self._secret()
        if not text.strip():
            return

        target_voice = quote(voice_id or self.default_voice_id, safe="")
        url = f"{self.base_url}/v1/text-to-speech/{target_voice}/stream"
        params = {"output_format": output_format}
        headers = {
            "xi-api-key": secret,
            "Content-Type": "application/json",
            "Accept": "audio/x-raw",
        }
        payload: dict[str, Any] = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": VOICE_SETTINGS,
        }
        code = self._language_code(language)
        if code:
            payload["language_code"] = code

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

    async def health(self) -> bool:
        if not self._api_key.get_secret_value():
            return False
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

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
