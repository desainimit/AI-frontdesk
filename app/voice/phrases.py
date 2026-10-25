"""Audio for phrases said on many calls, kept so they play at once.

The greeting and the short fixed lines ("one moment", "are you still there?") sound the
same every time, so the first call makes them and later calls replay the audio instead of
waiting for the speech service. The key covers everything that changes how a phrase
sounds; editing the greeting simply makes a new entry.
"""

import asyncio
import base64
import binascii
import hashlib
import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

TTL_SECONDS = 7 * 24 * 3600
# About fifteen seconds of µ-law audio; anything longer is not worth keeping.
MAX_BYTES = 120_000


class PhraseCache:
    def __init__(self, client: Redis, tenant_id: str, *, timeout_seconds: float = 0.3) -> None:
        self._client = client
        self._tenant = tenant_id
        self._timeout = timeout_seconds

    def _key(self, voice: str, language: str, text: str) -> str:
        digest = hashlib.sha256(f"{voice}\x00{language}\x00{text.strip()}".encode()).hexdigest()
        return f"tenant:{self._tenant}:phrase:{digest}"

    async def get(self, voice: str, language: str, text: str) -> bytes | None:
        try:
            # A slow cache must never hold up the greeting; the phrase is made instead.
            async with asyncio.timeout(self._timeout):
                value: object = await self._client.get(self._key(voice, language, text))
        except (RedisError, OSError, TimeoutError):
            return None
        if not isinstance(value, str):
            return None
        try:
            return base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None

    async def put(self, voice: str, language: str, text: str, audio: bytes) -> None:
        if not audio or len(audio) > MAX_BYTES:
            return
        try:
            await self._client.set(
                self._key(voice, language, text),
                base64.b64encode(audio).decode("ascii"),
                ex=TTL_SECONDS,
            )
        except (RedisError, OSError, TimeoutError):
            logger.warning("phrase_cache_write_failed")


class MemoryPhraseCache:
    """For local runs and tests, where there is no Redis."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], bytes] = {}

    async def get(self, voice: str, language: str, text: str) -> bytes | None:
        return self._items.get((voice, language, text.strip()))

    async def put(self, voice: str, language: str, text: str, audio: bytes) -> None:
        if audio and len(audio) <= MAX_BYTES:
            self._items[(voice, language, text.strip())] = audio
