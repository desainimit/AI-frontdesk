import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.conversation.state import ConversationState


class ConversationStoreError(RuntimeError):
    """Static error: never include Redis exceptions or serialized caller state."""


class ConversationStateStore(Protocol):
    async def get(self, tenant_id: UUID, call_id: UUID) -> ConversationState | None: ...
    async def save(self, state: ConversationState, ttl_seconds: int = 3600) -> None: ...
    async def delete(self, tenant_id: UUID, call_id: UUID) -> None: ...


def encode(state: ConversationState, ttl_seconds: int) -> str:
    if not 1 <= ttl_seconds <= 86400:
        raise ConversationStoreError("Invalid conversation TTL")
    validated = ConversationState.model_validate(state.model_dump())
    payload = validated.model_dump_json()
    if len(payload.encode()) > 65536:
        raise ConversationStoreError("Conversation state exceeds size limit")
    return payload


class MemoryConversationStateStore:
    """Bounded development/test store; production uses Redis."""

    def __init__(self) -> None:
        self._states: dict[tuple[UUID, UUID], tuple[str, datetime]] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant_id: UUID, call_id: UUID) -> ConversationState | None:
        async with self._lock:
            entry = self._states.get((tenant_id, call_id))
            if entry is None:
                return None
            payload, expires_at = entry
            if datetime.now(UTC) >= expires_at:
                self._states.pop((tenant_id, call_id), None)
                return None
            return ConversationState.model_validate_json(payload)

    async def save(self, state: ConversationState, ttl_seconds: int = 3600) -> None:
        payload = encode(state, ttl_seconds)
        async with self._lock:
            now = datetime.now(UTC)
            for key, (_, expiry) in list(self._states.items()):
                if now >= expiry:
                    del self._states[key]
            key = (state.tenant_id, state.call_id)
            if key not in self._states and len(self._states) >= 10000:
                raise ConversationStoreError("Conversation store is full")
            self._states[key] = (payload, now + timedelta(seconds=ttl_seconds))

    async def delete(self, tenant_id: UUID, call_id: UUID) -> None:
        async with self._lock:
            self._states.pop((tenant_id, call_id), None)


class RedisConversationStateStore:
    def __init__(self, redis_client: Redis) -> None:
        self._client = redis_client

    @staticmethod
    def _key(tenant_id: UUID, call_id: UUID) -> str:
        return f"tenant:{tenant_id}:call:{call_id}:conv_state"

    async def get(self, tenant_id: UUID, call_id: UUID) -> ConversationState | None:
        try:
            raw = await self._client.get(self._key(tenant_id, call_id))
            if raw is None:
                return None
            if len(raw) > 65536:
                raise ConversationStoreError("Conversation state exceeds size limit")
            state = ConversationState.model_validate_json(raw)
            if state.tenant_id != tenant_id or state.call_id != call_id:
                raise ConversationStoreError("Conversation key does not match state")
            return state
        except (RedisError, ValidationError, ValueError):
            raise ConversationStoreError("Conversation state unavailable") from None

    async def save(self, state: ConversationState, ttl_seconds: int = 3600) -> None:
        payload = encode(state, ttl_seconds)
        try:
            await self._client.set(
                self._key(state.tenant_id, state.call_id), payload, ex=ttl_seconds
            )
        except RedisError:
            raise ConversationStoreError("Conversation state unavailable") from None

    async def delete(self, tenant_id: UUID, call_id: UUID) -> None:
        try:
            await self._client.delete(self._key(tenant_id, call_id))
        except RedisError:
            raise ConversationStoreError("Conversation state unavailable") from None
