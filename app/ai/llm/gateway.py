"""One door to the model for a conversation turn: retries, a lighter model as a last
attempt, a circuit breaker per model account and a cap on one clinic's parallel turns."""

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import suppress
from uuid import UUID

from app.ai.llm.base import (
    ChatRequest,
    LLMCredentialsError,
    LLMCreditsError,
    LLMInterruptedError,
    LLMProvider,
    LLMProviderError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
    ModelEvent,
    TextDelta,
)
from app.voice.circuit import CircuitBreaker, CircuitRegistry

RETRIES = 2
# On a call: a request that has not started answering this long after it was sent gets
# a second, identical one beside it, and whichever answers first is used. A slow
# moment at the provider then costs a second, not ten.
HEDGE_AFTER_SECONDS = 1.8
# On a call: no first words this long after the request (both copies of it, when
# hedged), and the next attempt is made, then the caller is told. Kept here rather
# than as the provider's timeout, which Gemini sends to Google as a deadline it may
# refuse when short.
FIRST_WORDS_SECONDS = 8.0
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
        hedge_after: float | None = None,
        retries: int = RETRIES,
        first_words_within: float | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._breaker = breaker or CircuitBreaker(
            name="llm", failure_threshold=BREAKER_FAILURES, reset_seconds=BREAKER_RESET_SECONDS
        )
        self._slot = slot or asyncio.Semaphore(TURNS_PER_CLINIC)
        self._pause = pause_seconds
        self._hedge_after = hedge_after
        self._retries = retries
        self._first_words_within = first_words_within
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
            attempts = [self._primary] * (self._retries + 1)
            if self._fallback is not None:
                attempts.append(self._fallback)
            for number, model in enumerate(attempts):
                spoken = False
                try:
                    async for event in self._attempt(model, request):
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

    def _attempt(self, model: LLMProvider, request: ChatRequest) -> AsyncIterator[ModelEvent]:
        stream = (
            model.stream(request)
            if self._hedge_after is None
            else first_to_answer(model, request, self._hedge_after)
        )
        if self._first_words_within is None:
            return stream
        return within(stream, self._first_words_within)


async def within(stream: AsyncIterator[ModelEvent], seconds: float) -> AsyncIterator[ModelEvent]:
    """The stream, as long as its first event comes within `seconds`."""
    events = aiter(stream)
    try:
        async with asyncio.timeout(seconds):
            first = await anext(events)
    except StopAsyncIteration:
        return
    except TimeoutError:
        close = getattr(events, "aclose", None)
        if close is not None:
            with suppress(BaseException):
                await close()
        raise LLMTimeoutError("LLM did not start answering in time") from None
    yield first
    async for event in events:
        yield event


async def first_to_answer(
    model: LLMProvider, request: ChatRequest, hedge_after: float
) -> AsyncIterator[ModelEvent]:
    """The model's stream, or a second one started beside it when the first is slow to
    begin: whichever produces its first event first is used, the other is dropped."""
    first = aiter(model.stream(request))
    first_event = asyncio.ensure_future(anext(first))
    done, _ = await asyncio.wait({first_event}, timeout=hedge_after)
    racers = {first_event: first}
    if not done:
        second = aiter(model.stream(request))
        racers[asyncio.ensure_future(anext(second))] = second
    winner: AsyncIterator[ModelEvent] | None = None
    event: ModelEvent | None = None
    try:
        pending = set(racers)
        error: BaseException | None = None
        while pending and winner is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    winner, event = racers[task], task.result()
                    break
                # One failed: the other may still answer.
                error = error or task.exception()
        if winner is None:
            if isinstance(error, StopAsyncIteration):
                return
            assert error is not None
            raise error
    finally:
        for task, stream in racers.items():
            if stream is winner:
                continue
            task.cancel()
            with suppress(BaseException):
                await task
            close = getattr(stream, "aclose", None)
            if close is not None:
                with suppress(BaseException):
                    await close()
    assert event is not None and winner is not None
    yield event
    async for event in winner:
        yield event


def breaker_for(registry: CircuitRegistry, tenant_id: UUID, account: str) -> CircuitBreaker:
    """One breaker per model account: a clinic's own, or the platform's shared one."""
    key = f"llm:{account}" if account == "platform" else f"llm:{tenant_id}:{account}"
    return registry.get(key, BREAKER_FAILURES, BREAKER_RESET_SECONDS)
