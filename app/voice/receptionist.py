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
from app.ai.wording import spoken
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
from app.voice.pipeline import VoicePipeline
from app.voice.stt.base import TranscriptionResult
from app.voice.twilio import TwilioCredentials

logger = logging.getLogger(__name__)

# Silence longer than this before the first words is filled with "one moment".
FILLER_AFTER_SECONDS = 1.5
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

        # The reply is written in the background so the first sentence can be spoken
        # while the next is still on its way.
        producer = asyncio.create_task(produce())
        heard: list[str] = []
        handoff: Handoff | None = None
        done: Done | None = None
        filled = spoke = False
        fill_at = monotonic() + FILLER_AFTER_SECONDS
        try:
            while True:
                try:
                    wait = None if filled or spoke else max(0.05, fill_at - monotonic())
                    event = await asyncio.wait_for(events.get(), wait)
                except TimeoutError:
                    filled = True
                    await pipeline.speak(
                        spoken(self.state.language, "one_moment"), record=False, follow_on=True
                    )
                    continue
                if event is None:
                    break
                if isinstance(event, Say):
                    spoke = True
                    if await pipeline.speak(event.text, record=False, follow_on=True):
                        heard.append(event.text)
                elif isinstance(event, Working):
                    if not filled and not spoke:
                        filled = True
                        await pipeline.speak(
                            spoken(self.state.language, "one_moment"), record=False
                        )
                elif isinstance(event, Handoff):
                    handoff = event
                elif isinstance(event, Done):
                    done = event
        finally:
            # The caller spoke over the reply, or hung up: stop writing it at once.
            if not producer.done():
                producer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await producer
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
