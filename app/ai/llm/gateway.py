"""One door to the model for a conversation turn: retries, a lighter model as a last
attempt, a circuit breaker per model account and a cap on one clinic's parallel turns."""

import asyncio
import random
from collections.abc import AsyncIterator
from uuid import UUID

from app.ai.llm.base import (
    ChatRequest,
    LLMCredentialsError,
    LLMCreditsError,
    LLMInterruptedError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMUnavailableError,
    ModelEvent,
    TextDelta,
)
from app.voice.circuit import CircuitBreaker, CircuitRegistry

RETRIES = 2
BREAKER_FAILURES = 4
BREAKER_RESET_SECONDS = 30.0
# Clinics on the platform's key share its rate limit; one busy clinic must not use it up.
TURNS_PER_CLINIC = 8
WAIT_FOR_A_TURN_SECONDS = 3.0


class TurnLimits:
    """How many model turns one clinic may run at once in this process."""

    def __init__(self, per_clinic: int = TURNS_PER_CLINIC) -> None:
        self._per_clinic = per_clinic
        self._slots: dict[UUID, asyncio.Semaphore] = {}

    def slot(self, tenant_id: UUID) -> asyncio.Semaphore:
        if tenant_id not in self._slots:
            if len(self._slots) >= 4096:
                self._slots.pop(next(iter(self._slots)))
            self._slots[tenant_id] = asyncio.Semaphore(self._per_clinic)
        return self._slots[tenant_id]


class ModelGateway:
    def __init__(
        self,
        primary: LLMProvider,
        *,
        fallback: LLMProvider | None = None,
        breaker: CircuitBreaker | None = None,
        slot: asyncio.Semaphore | None = None,
        pause_seconds: float = 0.3,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._breaker = breaker or CircuitBreaker(
            name="llm", failure_threshold=BREAKER_FAILURES, reset_seconds=BREAKER_RESET_SECONDS
        )
        self._slot = slot or asyncio.Semaphore(TURNS_PER_CLINIC)
        self._pause = pause_seconds
        # Set when the account itself was refused, so someone can be told to fix it.
        self.account_problem: str | None = None

    async def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        if not self._breaker.allow_request():
            raise LLMUnavailableError("LLM is unavailable")
        try:
            async with asyncio.timeout(WAIT_FOR_A_TURN_SECONDS):
                await self._slot.acquire()
        except TimeoutError:
            raise LLMRateLimitError("Too many conversations at once") from None
        try:
            # The same model up to three times, then the lighter one once.
            attempts = [self._primary] * (RETRIES + 1)
            if self._fallback is not None:
                attempts.append(self._fallback)
            for number, model in enumerate(attempts):
                spoken = False
                try:
                    async for event in model.stream(request):
                        spoken = spoken or isinstance(event, TextDelta)
                        yield event
                    self._breaker.record_success()
                    return
                except LLMProviderError as error:
                    if isinstance(error, LLMCredentialsError | LLMCreditsError):
                        self.account_problem = error.code
                        self._breaker.record_failure()
                        raise
                    if spoken:
                        # Part of the reply is already out; saying it again would be worse.
                        self._breaker.record_failure()
                        raise LLMInterruptedError("LLM stream was interrupted") from None
                    if not error.retryable:
                        raise
                    self._breaker.record_failure()
                    if number == len(attempts) - 1 or not self._breaker.allow_request():
                        raise
                    await asyncio.sleep(self._pause * 2**number * (0.5 + random.random()))
        finally:
            self._slot.release()


def breaker_for(registry: CircuitRegistry, tenant_id: UUID, account: str) -> CircuitBreaker:
    """One breaker per model account: a clinic's own, or the platform's shared one."""
    key = f"llm:{account}" if account == "platform" else f"llm:{tenant_id}:{account}"
    return registry.get(key, BREAKER_FAILURES, BREAKER_RESET_SECONDS)
