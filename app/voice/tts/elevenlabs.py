import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from app.core.settings import SpeakingSpeed
from app.languages import known
from app.languages import language as registry_language
from app.voice.http import retry_after_seconds
from app.voice.tts.base import (
    TTSError,
    TTSPermanentError,
    TTSRateLimitError,
    TTSTimeoutError,
    TTSTransientError,
)
from app.voice.tts.elevenlabs_stream import ElevenLabsSocket
from app.voice.tts.speed import ELEVENLABS_SPEED

logger = logging.getLogger(__name__)

# Models ElevenLabs serves over its multi-context WebSocket; Eleven v3 is not one of them.
SOCKET_MODELS = frozenset({"eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2"})
# A little less stable than the default sounds livelier and less read-out.
VOICE_SETTINGS = {"stability": 0.45, "similarity_boost": 0.75}


class ElevenLabsTTSProvider:
    """Production ElevenLabs streaming TTS provider.

    One instance serves one call. Speech goes over one WebSocket opened as the call
    starts; if that socket fails, the rest of the call uses HTTP streaming on one reused
    connection instead, so a socket problem never silences or drops a call.
    """

    def __init__(
        self,
        api_key: SecretStr,
        *,
        speed: SpeakingSpeed = "normal",
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
        self.speed: SpeakingSpeed = speed
        self.voice_settings = {**VOICE_SETTINGS, "speed": ELEVENLABS_SPEED[speed]}
        self.output_format = output_format
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._socket: ElevenLabsSocket | None = None
        self._socket_key: tuple[str, str | None] | None = None
        self._retired: list[ElevenLabsSocket] = []
        # Set once the socket has failed on this call: HTTP from then on.
        self.socket_failed = model_id not in SOCKET_MODELS

    @property
    def name(self) -> str:
        return "elevenlabs"

    @property
    def voice_key(self) -> str:
        """What decides how a phrase sounds, for reusing audio already made."""
        key = f"elevenlabs:{self.model_id}:{self.default_voice_id}"
        # The pace itself, so audio made at an earlier pace is never replayed.
        return f"{key}:{ELEVENLABS_SPEED[self.speed]}"

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
        if not known(language):
            raise TTSPermanentError("Unsupported speech language")
        entry = registry_language(language)
        if self.model_id not in entry.elevenlabs_models:
            raise TTSPermanentError(f"{entry.name} needs another ElevenLabs model or Azure Speech")
        # Hinglish is read with Hindi pronunciation but not marked as Hindi text.
        return entry.elevenlabs

    def _speech_socket(self, voice_id: str | None, language: str | None) -> ElevenLabsSocket:
        key = (voice_id or self.default_voice_id, self._language_code(language))
        if self._socket is None or self._socket_key != key:
            if self._socket is not None:
                # A language switch needs a new connection; the old one closes with the call.
                self._retired.append(self._socket)
            self._socket = ElevenLabsSocket(
                api_key=self._secret(),
                voice_id=key[0],
                model_id=self.model_id,
                language_code=key[1],
                voice_settings=self.voice_settings,
                base_url=self.base_url,
                timeout_seconds=self.timeout_seconds,
            )
            self._socket_key = key
        return self._socket

    async def warm_up(self, voice_id: str | None = None, *, language: str | None = None) -> None:
        """Open the connection before the first words are needed (costs nothing)."""
        if not self.socket_failed:
            try:
                await self._speech_socket(voice_id, language).connect()
                return
            except TTSPermanentError:
                raise
            except TTSError:
                # Tried again on the first sentence; HTTP is ready meanwhile.
                pass
        with suppress(httpx.HTTPError, TTSError):
            await self._get_client().get(
                f"{self.base_url}/v1/models", headers={"xi-api-key": self._secret()}, timeout=3.0
            )

    async def synthesize_ulaw(
        self, text: str, voice_id: str | None = None, *, language: str | None = None
    ) -> AsyncIterator[bytes]:
        """µ-law 8 kHz audio, the format Twilio plays, over the call's WebSocket."""
        if not text.strip():
            return
        if not self.socket_failed:
            started = False
            try:
                async for chunk in self._speech_socket(voice_id, language).speak(text):
                    started = True
                    yield chunk
                return
            except TTSPermanentError:
                raise
            except TTSError as error:
                if started:
                    # Half a sentence was heard; the next one tries the socket again.
                    raise
                self.socket_failed = True
                logger.warning("elevenlabs_socket_fallback", extra={"error_type": error.code})
        # Imported here: app.voice.audio imports this package.
        from app.voice.audio import pcm_frames
        from app.voice.codec import pcm_to_ulaw

        async for frame in pcm_frames(self.synthesize(text, voice_id, language=language)):
            yield pcm_to_ulaw(frame)

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
            "voice_settings": self.voice_settings,
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
        sockets = [*self._retired, *([self._socket] if self._socket else [])]
        self._socket, self._retired = None, []
        for socket in sockets:
            with suppress(TTSError, OSError):
                await socket.close()
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
