from time import perf_counter
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from app.voice.http import retry_after_seconds
from app.voice.stt.base import (
    STTError,
    STTPermanentError,
    STTRateLimitError,
    STTStream,
    STTTimeoutError,
    STTTransientError,
    TranscriptionResult,
)
from app.voice.stt.flux import FluxLiveStream
from app.voice.stt.languages import language_counts
from app.voice.stt.live import DeepgramLiveStream

# Languages Flux Multilingual recognises; Gujarati is not one of them.
FLUX_LANGUAGES = frozenset({"en", "es", "fr", "de", "hi", "ru", "pt", "ja", "it", "nl"})
# Deepgram accepts up to 100 key terms per request.
MAX_KEYTERMS = 100


class DeepgramSTTProvider:
    """Deepgram live WebSocket recognition and explicit batch transcription.

    With `model="flux"` the live stream uses Flux, which also decides when the caller
    has finished speaking. Batch transcription always uses Nova 3, since Flux is live only.
    """

    def __init__(
        self,
        api_key: SecretStr,
        *,
        model: str = "nova-2",
        language: str = "en",
        keyterms: tuple[str, ...] = (),
        language_hints: tuple[str, ...] = (),
        base_url: str = "https://api.deepgram.com",
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.language = language
        # Names the caller is likely to say: the clinic, its providers and services.
        self.keyterms = tuple(dict.fromkeys(term.strip() for term in keyterms if term.strip()))[
            :MAX_KEYTERMS
        ]
        self.language_hints = tuple(code for code in language_hints if code in FLUX_LANGUAGES)
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    def _live_query(self, sample_rate: int, encoding: str) -> tuple[str, str]:
        """The live endpoint's path and query for the configured model."""
        terms = [("keyterm", term) for term in self.keyterms]
        if self.model == "flux":
            if self.language == "gu":
                raise STTPermanentError("Flux does not recognise Gujarati; use Nova 3")
            hints = (self.language,) if self.language in FLUX_LANGUAGES else self.language_hints
            english = self.language == "en"
            params: list[tuple[str, str | int]] = [
                ("model", "flux-general-en" if english else "flux-general-multi"),
                ("encoding", encoding),
                ("sample_rate", sample_rate),
                ("eot_threshold", "0.7"),
                ("eot_timeout_ms", 3000),
                # Dates, times and phone numbers come back as digits.
                ("numerals", "true"),
                *(() if english else (("language_hint", code) for code in hints)),
                *terms,
            ]
            return "/v2/listen", urlencode(params)
        params = [
            ("model", self.model),
            ("language", self.language),
            ("encoding", encoding),
            ("sample_rate", sample_rate),
            ("channels", 1),
            ("interim_results", "true"),
            # Half a second: a caller who pauses for breath is not answered too early.
            ("endpointing", 500),
            ("utterance_end_ms", 1000),
            ("smart_format", "true"),
            *(terms if self.model.startswith("nova-3") else ()),
        ]
        return "/v1/listen", urlencode(params)

    @property
    def name(self) -> str:
        return "deepgram"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        if not self._api_key.get_secret_value():
            raise STTPermanentError("Deepgram API key is not configured")
        if encoding != "linear16" or sample_rate not in {8000, 16000}:
            raise STTPermanentError("Unsupported speech input format")
        path, params = self._live_query(sample_rate, encoding)
        origin = self.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        try:
            socket = await connect(
                origin + path + "?" + params,
                additional_headers={"Authorization": "Token " + self._api_key.get_secret_value()},
                open_timeout=self.timeout_seconds,
                close_timeout=2,
                max_size=65536,
                max_queue=16,
            )
        except InvalidStatus as error:
            status = error.response.status_code
            if status in {401, 403}:
                raise STTPermanentError("Deepgram authentication failed") from None
            if status == 429:
                raise STTRateLimitError(retry_after=5) from None
            raise STTTransientError("Deepgram handshake failed") from None
        except (WebSocketException, OSError, TimeoutError):
            raise STTTransientError("Deepgram connection failed") from None
        if self.model == "flux":
            return FluxLiveStream(socket, self.timeout_seconds)
        return DeepgramLiveStream(socket, self.language, self.timeout_seconds)

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        secret = self._api_key.get_secret_value()
        if not secret:
            raise STTPermanentError("Deepgram API key is not configured")
        if not audio:
            return TranscriptionResult(text="", is_final=True, confidence=1.0)

        url = f"{self.base_url}/v1/listen"
        params: dict[str, Any] = {
            "model": "nova-3" if self.model == "flux" else self.model,
            "smart_format": "true",
            "punctuate": "true",
            "encoding": "linear16",
            "sample_rate": sample_rate,
            "channels": 1,
        }
        if self.language:
            params["language"] = self.language

        headers = {
            "Authorization": f"Token {secret}",
            "Content-Type": f"audio/raw;encoding=linear16;sample_rate={sample_rate}",
        }

        started = perf_counter()
        client = self._get_client()
        try:
            response = await client.post(
                url,
                params=params,
                headers=headers,
                content=audio,
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise STTTimeoutError("Deepgram request timed out") from exc
        except httpx.RequestError as exc:
            raise STTTransientError("Deepgram connection error") from exc

        duration_ms = (perf_counter() - started) * 1000.0

        if response.status_code == 429:
            retry_after = retry_after_seconds(response.headers.get("retry-after"))
            raise STTRateLimitError(retry_after=retry_after)
        if response.status_code in (401, 403):
            raise STTPermanentError("Deepgram authentication failed")
        if response.status_code >= 500:
            raise STTTransientError(f"Deepgram server returned {response.status_code}")
        if response.status_code >= 400:
            raise STTPermanentError(f"Deepgram rejected request with {response.status_code}")

        try:
            data = response.json()
            channel = data.get("results", {}).get("channels", [{}])[0]
            alt = channel.get("alternatives", [{}])[0]
            transcript = alt.get("transcript", "")
            confidence = float(alt.get("confidence", 0.0))
            counts = language_counts(channel, alt)
            languages = tuple(code for code, _ in counts.most_common())
            detected_lang = languages[0] if languages else None
            return TranscriptionResult(
                text=transcript,
                is_final=True,
                confidence=confidence,
                language=detected_lang,
                duration_ms=duration_ms,
                languages=languages,
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise STTTransientError("Invalid response structure from Deepgram") from exc

    async def health(self) -> bool:
        if not self._api_key.get_secret_value():
            return False
        try:
            stream = await self.create_stream()
            await stream.close()
            return True
        except (STTError, OSError, TimeoutError):
            return False
