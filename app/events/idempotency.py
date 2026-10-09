import hashlib
import json
import re
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import ActorType, RequestContext
from app.core.errors import ApplicationError, AuthorizationError, ConflictError
from app.events.models import IdempotencyRecord


class IdempotencyConflictError(ConflictError):
    code = "idempotency_key_reused"
    public_message = "This idempotency key was already used with a different request."


class InvalidIdempotencyKeyError(ApplicationError):
    status_code = 422
    code = "invalid_idempotency_key"
    public_message = (
        "Use an idempotency key of 1-128 letters, digits, dots, colons, underscores or hyphens."
    )


class IdempotencyRepository:
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session, self._context = session, context
        self._key: str | None = None
        self._digest: str | None = None

    async def replay[T: BaseModel](
        self,
        key: str | None,
        operation: str,
        command: BaseModel,
        response_type: type[T],
        resource_id: UUID | None = None,
    ) -> T | None:
        if key is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise InvalidIdempotencyKeyError()
        is_caller = self._context.actor_type == ActorType.CALLER
        is_support = self._context.actor_type == ActorType.PLATFORM_SUPPORT
        principal = (
            self._context.support_session_id
            if is_support
            else self._context.call_id
            if is_caller
            else self._context.actor_id
        )
        if principal is None:
            raise AuthorizationError()
        self._key = hashlib.sha256(key.encode()).hexdigest()
        payload = {
            "operation": operation,
            "resource_id": str(resource_id) if resource_id else None,
            "command": command.model_dump(mode="json"),
        }
        self._digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        scope = f"{self._context.tenant_id}:{principal}:{self._key}"
        lock = int.from_bytes(hashlib.sha256(scope.encode()).digest()[:8], "big", signed=True)
        # Transaction-scoped serialization covers both creation and response recording.
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
        record = await self._session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == self._context.tenant_id,
                (IdempotencyRecord.call_id == self._context.call_id)
                if is_caller
                else (IdempotencyRecord.support_session_id == self._context.support_session_id)
                if is_support
                else (IdempotencyRecord.actor_id == self._context.actor_id),
                IdempotencyRecord.key_digest == self._key,
            )
        )
        if record is None:
            return None
        if record.request_digest != self._digest:
            raise IdempotencyConflictError()
        return response_type.model_validate(record.response)

    def save[T: BaseModel](self, response: T) -> T:
        if self._key is not None and self._digest is not None:
            self._session.add(
                IdempotencyRecord(
                    tenant_id=self._context.tenant_id,
                    actor_id=self._context.actor_id,
                    platform_user_id=self._context.platform_user_id,
                    support_session_id=self._context.support_session_id,
                    call_id=self._context.call_id
                    if self._context.actor_type == ActorType.CALLER
                    else None,
                    key_digest=self._key,
                    request_digest=self._digest,
                    response=response.model_dump(mode="json"),
                )
            )
        return response
