"""What happens after the caller finishes speaking: the reply, spoken sentence by
sentence while the rest is still being written, and whatever has to follow it."""

import asyncio
import logging
from contextlib import suppress
from time import monotonic

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.engine import Done, Engine, Event, Handoff, Say, Working
from app.ai.session import Session
from app.ai.usage import record_turn
from app.ai.wording import filler
from app.audit.events import AuditAction
from app.calls.service import CallService
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, SafetyDecision
from app.conversation.store import ConversationStateStore
from app.core.database.rls import set_tenant_scope
from app.core.errors import ApplicationError
from app.core.settings import Settings
from app.safety.messages import transfer_failed_message
from app.safety.transfer import TransferError, transfer_call
from app.tickets.service import open_ticket
from app.voice.metrics import VoiceMetrics
from app.voice.pipeline import Speech, VoicePipeline
from app.voice.stt.base import TranscriptionResult
from app.voice.twilio import TwilioCredentials

logger = logging.getLogger(__name__)

# Silence longer than this before the first words is filled with a short "let me see".
FILLER_AFTER_SECONDS = 1.5
# A lookup that has not answered by now gets the same, unless something was said.
LOOKUP_FILLER_AFTER_SECONDS = 0.8
TICKETS = {
    "requested": "Caller asked to speak to someone",
    "emergency": "Possible emergency on a call",
    "unavailable": "The receptionist could not finish a call",
}


class CallReceptionist:
    def __init__(
        self,
        *,
        engine: Engine,
        session: Session,
        state: ConversationState,
        store: ConversationStateStore,
        store_ttl_seconds: int,
        service: CallService,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        account: TwilioCredentials,
        metrics: VoiceMetrics,
    ) -> None:
        self.engine = engine
        self.session = session
        self.state = state
        self.store = store
        self.store_ttl_seconds = store_ttl_seconds
        self.service = service
        self.sessions = sessions
        self.settings = settings
        self.account = account
        self.metrics = metrics
        self.pipeline: VoicePipeline | None = None
        # Set once the call has been put through to a person.
        self.transferred = False
        self._fillers = 0

    async def reply(self, result: TranscriptionResult) -> None:
        pipeline = self.pipeline
        if pipeline is None or not result.is_final or not result.text.strip():
            return
        events: asyncio.Queue[Event | None] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for event in self.engine.run(self.session, self.state, result.text):
                    events.put_nowait(event)
            finally:
                events.put_nowait(None)

        # Each sentence's audio is made as soon as the sentence is written and played in
        # order, so the next is ready before the current one ends.
        queue: asyncio.Queue[tuple[Speech, bool] | None] = asyncio.Queue()
        heard: list[str] = []
        prepared: list[Speech] = []

        async def play() -> None:
            while (item := await queue.get()) is not None:
                speech, record = item
                if not await pipeline.play(speech, follow_on=True):
                    return
                if record:
                    heard.append(speech.text)

        async def enqueue(text: str, *, record: bool = True, cache: bool = False) -> None:
            speech = await pipeline.prepare(text, cache=cache)
            prepared.append(speech)
            queue.put_nowait((speech, record))

        async def say_filler() -> None:
            self._fillers += 1
            await enqueue(filler(self.state.language, self._fillers), record=False, cache=True)

        # The reply is written in the background so the first sentence can be spoken
        # while the next is still on its way.
        producer = asyncio.create_task(produce())
        player = asyncio.create_task(play())
        handoff: Handoff | None = None
        done: Done | None = None
        # When a short "let me check" fills the silence, if nothing has been said by then.
        # At most one per reply.
        fill_at: float | None = monotonic() + FILLER_AFTER_SECONDS
        try:
            while True:
                try:
                    wait = None if fill_at is None else max(0.0, fill_at - monotonic())
                    event = await asyncio.wait_for(events.get(), wait)
                except TimeoutError:
                    fill_at = None
                    await say_filler()
                    continue
                if event is None:
                    break
                if isinstance(event, Say):
                    fill_at = None
                    if event.urgent:
                        # A safety alarm: whatever is still queued or playing gives way.
                        player.cancel()
                        with suppress(asyncio.CancelledError):
                            await player
                        for speech in prepared:
                            speech.cancel()
                        await pipeline.speak(event.text, record=False)
                        heard.append(event.text)
                        player = asyncio.create_task(play())
                        continue
                    await enqueue(event.text)
                elif isinstance(event, Working):
                    if fill_at is not None:
                        # Most lookups answer within a moment; only a slow one is filled.
                        fill_at = min(fill_at, monotonic() + LOOKUP_FILLER_AFTER_SECONDS)
                elif isinstance(event, Handoff):
                    handoff = event
                elif isinstance(event, Done):
                    done = event
            queue.put_nowait(None)
            await player
        finally:
            # The caller spoke over the reply, or hung up: stop writing it at once.
            for task in (producer, player):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            for speech in prepared:
                speech.cancel()
            if heard:
                # Only what the caller actually heard counts as having been said.
                text = " ".join(heard)
                ConversationStateManager.record_agent_response(self.state, text)
                self.state.append_transcript_turn("agent", text)
                with suppress(Exception):
                    await self.store.save(self.state, self.store_ttl_seconds)
            if done is not None:
                self.metrics.record_llm_latency(float(done.total_ms), done.model or "model")
                await record_turn(self.sessions, self.session, done)
        if handoff is not None:
            await self._hand_over(pipeline, handoff)

    async def _hand_over(self, pipeline: VoicePipeline, handoff: Handoff) -> None:
        safety = self.state.safety_state
        if handoff.reason == "emergency":
            if not safety.event_recorded:
                safety.event_recorded = True
                self.metrics.record_emergency(safety.source or "rule")
                await self._audit(AuditAction.EMERGENCY_DETECTED)
                await self._ticket("emergency", priority="urgent")
            return
        number = self.session.configuration.calls.transfer_number
        if safety.transfer_attempted:
            return
        safety.transfer_attempted = True
        await self._audit(AuditAction.HUMAN_TRANSFER_REQUESTED)
        # A ticket either way: if the transfer fails, or nobody picks up, the request
        # is still waiting for the team.
        await self._ticket(handoff.reason)
        if number is None:
            self.metrics.record_human_transfer("unavailable")
            return
        try:
            await transfer_call(self.settings, self.session.call, number, credentials=self.account)
        except TransferError:
            self.metrics.record_human_transfer("failed")
            safety.decision = SafetyDecision.ESCALATE
            await pipeline.speak(transfer_failed_message(self.state.language))
            await self._audit(AuditAction.HUMAN_TRANSFER_FAILED)
            return
        safety.transfer_confirmed = True
        self.transferred = True
        self.metrics.record_human_transfer("redirected")
        await self._audit(AuditAction.HUMAN_TRANSFER_REDIRECTED)

    async def _audit(self, action: AuditAction) -> None:
        try:
            await self.service.record_safety_event(self.session.call, action)
        except (SQLAlchemyError, ApplicationError):
            logger.warning(
                "safety_audit_failed", extra={"call_id": str(self.session.conversation_id)}
            )

    async def _ticket(self, reason: str, *, priority: str = "high") -> None:
        identity = self.state.identity_state
        try:
            async with self.sessions.begin() as database:
                await set_tenant_scope(database, self.session.tenant_id)
                await open_ticket(
                    database,
                    self.session.tenant_id,
                    source="call",
                    subject=TICKETS[reason],
                    description=(self.state.last_caller_utterance or "")[:500],
                    priority=priority,
                    patient_id=identity.patient_id if identity.is_verified else None,
                    contact=self.session.contact,
                    call_id=self.session.conversation_id,
                    dedupe_key=f"call:{self.session.conversation_id}:{reason}",
                    author="assistant",
                )
        except SQLAlchemyError:
            logger.error(
                "call_ticket_not_opened", extra={"call_id": str(self.session.conversation_id)}
            )
