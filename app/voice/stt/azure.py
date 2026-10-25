import io
import json
import wave
from collections import Counter
from collections.abc import Callable
from time import perf_counter
from typing import Any, cast

import httpx
from pydantic import SecretStr

from app.conversation.locales import LOCALES, normalize_language
from app.voice.codec import double_rate
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
        candidate_languages: tuple[str, ...] = (),
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._speech_key = speech_key
        self.region = region
        self.language = language
        self.language_resolver = language_resolver
        supported = {locale.speech_locale for locale in LOCALES.values()}
        if candidate_languages and (
            not 2 <= len(candidate_languages) <= 10
            or len(set(candidate_languages)) != len(candidate_languages)
            or not set(candidate_languages) <= supported
        ):
            raise ValueError("Choose distinct supported recognition locales")
        self.candidate_languages = candidate_languages
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
        pcm = double_rate(audio) if sample_rate == 8000 else audio
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
        if self.candidate_languages:
            # The short-audio endpoint accepts one locale only. Detect from audio,
            # not from text already decoded using the previous turn's language.
            url = (
                f"https://{self.region}.api.cognitive.microsoft.com"
                "/speechtotext/transcriptions:transcribe?api-version=2025-10-15"
            )
            del headers["Content-Type"]

        started = perf_counter()
        client = self._get_client()
        try:
            if self.candidate_languages:
                response = await client.post(
                    url,
                    headers=headers,
                    files={
                        "audio": ("utterance.wav", buffer.getvalue(), "audio/wav"),
                        "definition": (
                            None,
                            json.dumps({"locales": list(self.candidate_languages)}),
                            "application/json",
                        ),
                    },
                    timeout=self.timeout_seconds,
                )
            else:
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
            if self.candidate_languages:
                return self._automatic_result(data, duration_ms)
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

    def _automatic_result(self, data: dict[str, Any], duration_ms: float) -> TranscriptionResult:
        phrases = data["phrases"]
        if not isinstance(phrases, list):
            raise ValueError("Invalid recognition phrases")
        texts: list[str] = []
        confidence: list[float] = []
        counts: Counter[str] = Counter()
        for phrase in cast(list[dict[str, Any]], phrases):
            text, locale = phrase["text"], phrase["locale"]
            if not isinstance(text, str) or locale not in self.candidate_languages:
                raise ValueError("Unexpected recognition locale or text")
            if not text.strip():
                continue
            texts.append(text.strip())
            score = float(phrase.get("confidence", 0.0))
            if not 0.0 <= score <= 1.0:
                raise ValueError("Invalid recognition confidence")
            confidence.append(score)
            counts[normalize_language(locale)] += max(1, len(text.split()))
        languages = tuple(code for code, _ in counts.most_common())
        return TranscriptionResult(
            text=" ".join(texts),
            is_final=True,
            confidence=sum(confidence) / len(confidence) if confidence else 0.0,
            language=languages[0] if languages else None,
            languages=languages,
            duration_ms=duration_ms,
        )

    async def health(self) -> bool:
        return bool(self._speech_key.get_secret_value() and self.region)
