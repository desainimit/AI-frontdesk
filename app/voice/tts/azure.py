import re
from collections.abc import AsyncIterator
from xml.sax.saxutils import escape, quoteattr

import httpx
from pydantic import SecretStr

from app.conversation.locales import LOCALES, base_language
from app.voice.http import retry_after_seconds
from app.voice.tts.base import (
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)


class AzureTTSProvider:
    """Fallback Azure Cognitive Services Text-to-Speech streaming provider."""

    def __init__(
        self,
        speech_key: SecretStr,
        region: str = "eastus",
        *,
        default_voice: str = "en-US-JennyNeural",
        hindi_voice: str = "hi-IN-SwaraNeural",
        output_format: str = "raw-8khz-16bit-mono-pcm",
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._speech_key = speech_key
        self.region = region
        self.default_voice = default_voice
        self.hindi_voice = hindi_voice
        self.output_format = output_format
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    @property
    def name(self) -> str:
        return "azure"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def synthesize(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        secret = self._speech_key.get_secret_value()
        if not secret:
            raise TTSPermanentError("Azure Speech key is not configured")
        if not text.strip():
            return

        code = base_language(language) if language else "en"
        if code not in LOCALES:
            raise TTSPermanentError("No speech voice is configured for this language")
        selected_voice = (
            self.default_voice
            if code == "en"
            else self.hindi_voice
            if code == "hi"
            else LOCALES[code].azure_voice
        )
        target_voice = voice_id or selected_voice
        match = re.match(r"^([a-z]{2,3}-[A-Z]{2})-", target_voice)
        voice_locale = match.group(1) if match else "en-US"
        url = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = {
            "Ocp-Apim-Subscription-Key": secret,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": self.output_format,
            "User-Agent": "ai-frontdesk",
        }
        # Escape XML characters in speech text
        escaped = escape(text)
        ssml = (
            f"<speak version='1.0' xml:lang={quoteattr(voice_locale)}>"
            f"<voice xml:lang={quoteattr(voice_locale)} name={quoteattr(target_voice)}>"
            f"{escaped}</voice>"
            f"</speak>"
        )

        client = self._get_client()
        try:
            async with client.stream(
                "POST",
                url,
                headers=headers,
                content=ssml.encode("utf-8"),
                timeout=self.timeout_seconds,
            ) as response:
                if response.status_code == 429:
                    retry_after = retry_after_seconds(response.headers.get("retry-after"))
                    raise TTSRateLimitError(retry_after=retry_after)
                if response.status_code in (401, 403):
                    raise TTSPermanentError("Azure Speech TTS authentication failed")
                if response.status_code >= 500:
                    raise TTSTransientError(f"Azure Speech returned {response.status_code}")
                if response.status_code >= 400:
                    raise TTSPermanentError(
                        f"Azure Speech rejected request with {response.status_code}"
                    )

                async for chunk in response.aiter_bytes(chunk_size=1024):
                    if chunk:
                        yield chunk
        except httpx.TimeoutException as exc:
            raise TTSTimeoutError("Azure Speech TTS timed out") from exc
        except httpx.RequestError as exc:
            raise TTSTransientError("Azure Speech connection error") from exc

    async def health(self) -> bool:
        return bool(self._speech_key.get_secret_value() and self.region)
