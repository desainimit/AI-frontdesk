import audioop
import io
import wave
from collections.abc import Callable
from time import perf_counter
from typing import Any, cast

import httpx
from pydantic import SecretStr

from app.voice.http import retry_after_seconds
from app.voice.stt.base import (
    STTPermanentError,
    STTRateLimitError,
    STTStream,
    STTTimeoutError,
    STTTransientError,
    TranscriptionResult,
)
from app.voice.stt.utterance import UtteranceSTTStream


class AzureSTTProvider:
    """Fallback Azure Cognitive Services Speech-to-Text provider."""

    def __init__(
        self,
        speech_key: SecretStr,
        region: str = "eastus",
        *,
        language: str = "en-US",
        language_resolver: Callable[[], str] | None = None,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._speech_key = speech_key
        self.region = region
        self.language = language
        self.language_resolver = language_resolver
        self.timeout_seconds = timeout_seconds
        self._client = client

    @property
    def name(self) -> str:
        return "azure"

    def _get_client(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=self.timeout_seconds)

    async def create_stream(self, sample_rate: int = 8000, encoding: str = "linear16") -> STTStream:
        if not self._speech_key.get_secret_value():
            raise STTPermanentError("Azure Speech key is not configured")
        return UtteranceSTTStream(self, sample_rate, encoding)

    async def transcribe(self, audio: bytes, sample_rate: int = 8000) -> TranscriptionResult:
        secret = self._speech_key.get_secret_value()
        if not secret:
            raise STTPermanentError("Azure Speech key is not configured")
        if not audio:
            return TranscriptionResult(text="", is_final=True, confidence=1.0)

        if sample_rate not in {8000, 16000} or len(audio) % 2:
            raise STTPermanentError("Unsupported PCM input")
        pcm = audioop.ratecv(audio, 2, 1, sample_rate, 16000, None)[0]
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        recognition_language = self.language_resolver() if self.language_resolver else self.language
        url = (
            f"https://{self.region}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
            f"?language={recognition_language}&format=detailed"
        )
        headers = {
            "Ocp-Apim-Subscription-Key": secret,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
            "Accept": "application/json",
        }

        started = perf_counter()
        owns_client = self._client is None
        client = self._get_client()
        try:
            response = await client.post(
                url,
                headers=headers,
                content=buffer.getvalue(),
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise STTTimeoutError("Azure Speech STT timed out") from exc
        except httpx.RequestError as exc:
            raise STTTransientError("Azure Speech connection error") from exc
        finally:
            if owns_client:
                await client.aclose()

        duration_ms = (perf_counter() - started) * 1000.0

        if response.status_code == 429:
            retry_after = retry_after_seconds(response.headers.get("retry-after"))
            raise STTRateLimitError(retry_after=retry_after)
        if response.status_code in (401, 403):
            raise STTPermanentError("Azure Speech authentication failed")
        if response.status_code >= 500:
            raise STTTransientError(f"Azure Speech returned {response.status_code}")
        if response.status_code >= 400:
            raise STTPermanentError(f"Azure Speech rejected request with {response.status_code}")

        try:
            data: dict[str, Any] = response.json()
            status = data.get("RecognitionStatus")
            if status not in {
                "Success",
                "NoMatch",
                "InitialSilenceTimeout",
                "BabbleTimeout",
                "EndOfDictation",
            }:
                raise STTTransientError("Azure recognition failed")
            if status != "Success":
                # Expected silence/no-match is not a provider failure.
                return TranscriptionResult(
                    text="",
                    is_final=True,
                    confidence=0.0,
                    language=None,
                    duration_ms=duration_ms,
                )

            text = str(data.get("DisplayText") or "")
            confidence = 1.0
            nbest = cast(list[dict[str, Any]] | None, data.get("NBest"))
            if isinstance(nbest, list) and nbest:
                entry = nbest[0]
                if "Display" in entry:
                    text = str(entry["Display"])
                if "Confidence" in entry:
                    confidence = float(entry["Confidence"])
            return TranscriptionResult(
                text=text,
                is_final=True,
                confidence=confidence,
                language=None,
                duration_ms=duration_ms,
            )
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise STTTransientError("Invalid response structure from Azure Speech") from exc

    async def health(self) -> bool:
        return bool(self._speech_key.get_secret_value() and self.region)
