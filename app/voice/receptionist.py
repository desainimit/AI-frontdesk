"""What happens after the caller finishes speaking: the reply, spoken sentence by
sentence while the rest is still being written, and whatever has to follow it."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from time import monotonic

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.engine import Done, Engine, Event, Handoff, Say, Working
from app.ai.llm.base import LLMProvider
from app.ai.memory import summarize_call, summarize_earlier
from app.ai.prompts import HISTORY_TURNS
from app.ai.session import Session
from app.ai.usage import record_turn
from app.ai.wording import filler, spoken
from app.audit.events import AuditAction
from app.calls.service import CallService
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, SafetyDecision, TranscriptTurn
from app.conversation.store import ConversationStateStore
from app.core.database.rls import set_tenant_scope
from app.core.errors import ApplicationError
from app.core.settings import Settings
from app.safety import handover
from app.safety.messages import transfer_failed_message
from app.safety.transfer import TransferError, hang_up, transfer_call
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
# Recognition this unsure of what was said is asked for again, not answered.
UNCLEAR_BELOW = 0.5
# Turns out of the model's view are summarized in batches of this many.
SUMMARY_EVERY_TURNS = 10
SUMMARY_SECONDS = 8.0
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
        summarizer: LLMProvider | None = None,
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
        # What a noise interrupted, said once the caller turns out to have said nothing.
        self._unsaid: list[str] = []
        # The last turn was too unclear to answer and the caller was asked to repeat it.
        self._asked_again = False
        # The quicker model: the running summary of a long call, and the call summary.
        self.summarizer = summarizer
        self._summarizing: asyncio.Task[None] | None = None
        # The receptionist ended the call after saying goodbye.
        self.ended = False
        # Tickets opened during the call, so an after-hours call is not ticketed twice.
        self.tickets = 0
        # When the caller's last words ended, for the time until the reply was heard.
        self._turn_started: float | None = None

    async def reply(self, result: TranscriptionResult) -> None:
        if self.pipeline is None or not result.is_final or not result.text.strip():
            return
        self._unsaid = []
        self._turn_started = monotonic()
        if result.confidence < UNCLEAR_BELOW and not self._asked_again:
            # Probably misheard: ask once rather than answer the wrong thing. A second
            # unclear answer goes to the model, which can still ask in its own words.
            self._asked_again = True
            await self._respond(self._lines(spoken(self.state.language, "line_unclear")))
            return
        self._asked_again = False
        await self._respond(self.engine.run(self.session, self.state, result.text))

    @staticmethod
    async def _lines(*texts: str) -> AsyncIterator[Event]:
        for text in texts:
            yield Say(text)

    async def resume(self) -> None:
        """A noise stopped the reply and the caller said nothing: finish it."""
        unsaid, self._unsaid = self._unsaid, []
        if self.pipeline is None or not unsaid:
            return

        self.metrics.record_false_interruption()
        await self._respond(self._lines(*unsaid), fillers=False)

    async def silence(self, times: int) -> None:
        """The caller has said nothing for a while: check they are there, then let them go."""
        if self.pipeline is None or self.transferred or self.ended:
            return
        if times == 1:
            await self._respond(
                self._lines(spoken(self.state.language, "still_there")), fillers=False
            )
            return
        await self._respond(
            self._lines(spoken(self.state.language, "goodbye_silent")), fillers=False
        )
        await self._end_call("caller_silent")

    async def _end_call(self, reason: str) -> None:
        """Hang up once the goodbye has been heard."""
        if self.ended or self.transferred:
            return
        self.ended = True
        try:
            await hang_up(self.settings, self.session.call, credentials=self.account)
        except (TransferError, ValueError):
            # The caller can still hang up themselves.
            logger.warning("call_hang_up_failed", extra={"reason": reason})
            self.ended = False

    async def _respond(self, source: AsyncIterator[Event], *, fillers: bool = True) -> None:
        pipeline = self.pipeline
        if pipeline is None:
            return
        events: asyncio.Queue[Event | None] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for event in source:
                    events.put_nowait(event)
            finally:
                events.put_nowait(None)

        # Each sentence's audio is made as soon as the sentence is written and played in
        # order, so the next is ready before the current one ends.
        queue: asyncio.Queue[Speech | None] = asyncio.Queue()
        # Everything queued to be said, in order; False marks a filler, not remembered.
        items: list[tuple[Speech, bool]] = []
        finished = False

        async def play() -> None:
            while (speech := await queue.get()) is not None:
                if not await pipeline.play(speech, follow_on=True):
                    return

        async def enqueue(text: str, *, record: bool = True, cache: bool = False) -> None:
            speech = await pipeline.prepare(text, cache=cache)
            items.append((speech, record))
            queue.put_nowait(speech)

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
        fill_at: float | None = monotonic() + FILLER_AFTER_SECONDS if fillers else None
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
                        for speech, _ in items:
                            if not speech.played:
                                speech.cancel()
                        items = [(speech, record) for speech, record in items if speech.played]
                        queue = asyncio.Queue()
                        alarm = await pipeline.prepare(event.text)
                        items.append((alarm, True))
                        await pipeline.play(alarm)
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
            # The reply is over once the caller has heard its last word; a barge-in
            # before then still counts as talking over it.
            await pipeline.wait_played()
            finished = True
        finally:
            # The caller spoke over the reply, or hung up: stop writing it at once.
            for task in (producer, player):
                if not task.done():
                    task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            for speech, _ in items:
                speech.cancel()
            self._remember(items, finished, pipeline.interrupted_at)
            with suppress(Exception):
                await self.store.save(self.state, self.store_ttl_seconds)
            first_audio = self._first_audio(items)
            if first_audio is not None:
                self.metrics.record_first_audio(first_audio)
            if done is not None:
                self.metrics.record_llm_latency(float(done.total_ms), done.model or "model")
                await record_turn(
                    self.sessions,
                    self.session,
                    done,
                    first_audio_ms=first_audio,
                    interrupted=not finished,
                )
            self._keep_summary()
        if handoff is not None:
            await self._hand_over(pipeline, handoff)
        elif finished and self.state.end_requested:
            await self._end_call("goodbye")

    def _first_audio(self, items: list[tuple[Speech, bool]]) -> int | None:
        """Milliseconds from the end of the caller's words to the first sound of the reply."""
        started, self._turn_started = self._turn_started, None
        heard = [speech.started_at for speech, _ in items if speech.started_at is not None]
        if started is None or not heard:
            return None
        return max(0, int((min(heard) - started) * 1000))

    def _keep_summary(self) -> None:
        """Summarize, in the background, turns that have moved out of the model's view."""
        state = self.state
        hidden_until = state.turns_appended - HISTORY_TURNS
        if (
            self.summarizer is None
            or hidden_until - state.summarized_through < SUMMARY_EVERY_TURNS
            or (self._summarizing is not None and not self._summarizing.done())
        ):
            return
        first_kept = state.turns_appended - len(state.transcript_turns)
        start = max(state.summarized_through, first_kept) - first_kept
        turns = state.transcript_turns[start : hidden_until - first_kept]
        self._summarizing = asyncio.create_task(self._summarize(turns, hidden_until))

    async def _summarize(self, turns: list[TranscriptTurn], through: int) -> None:
        if self.summarizer is None or not turns:
            return
        try:
            async with asyncio.timeout(SUMMARY_SECONDS):
                summary = await summarize_earlier(
                    self.summarizer, self.state.earlier_summary, turns
                )
        except Exception:
            # The model still has the recent turns; the summary is tried again later.
            logger.warning("call_summary_update_failed")
            return
        self.state.earlier_summary = summary[:2000]
        self.state.summarized_through = through

    async def call_summary(self) -> str | None:
        """The summary staff read once the call is over, written by the quicker model."""
        if self._summarizing is not None and not self._summarizing.done():
            self._summarizing.cancel()
        turns = self.state.transcript_turns[-80:]
        if self.summarizer is None or len(turns) < 2:
            return None
        try:
            async with asyncio.timeout(SUMMARY_SECONDS):
                return await summarize_call(self.summarizer, turns)
        except Exception:
            # The rule-based summary is used instead.
            logger.warning("call_summary_failed")
            return None

    def _remember(
        self, items: list[tuple[Speech, bool]], finished: bool, stopped_at: float | None
    ) -> None:
        """Only what the caller actually heard counts as having been said.

        A sentence they talked over is kept as far as they heard it, and the model is
        told the rest went unsaid, so it neither repeats the whole reply nor assumes
        the caller heard the end of it.
        """
        heard: list[str] = []
        unsaid: list[str] = []
        cut = False
        for speech, record in items:
            if not record:
                continue
            if finished or speech.played:
                heard.append(speech.text)
                continue
            if unsaid or speech.started_at is None:
                unsaid.append(speech.text)
                continue
            # Talked over while it played.
            cut = True
            partial = speech.heard(stopped_at or monotonic())
            if partial:
                heard.append(partial + " —")
            unsaid.append(speech.text)
        if heard:
            text = " ".join(heard)
            ConversationStateManager.record_agent_response(self.state, text)
            self.state.append_transcript_turn("agent", text)
        # A reply cut off before a word of it played was never said at all.
        cut = cut or bool(heard and unsaid)
        self.state.reply_cut_off = cut
        self.state.unsaid = " ".join(unsaid)[:1000] if cut else None
        self._unsaid = unsaid if cut else []

    async def _hand_over(self, pipeline: VoicePipeline, handoff: Handoff) -> None:
        safety = self.state.safety_state
        if handoff.reason == "emergency":
            if not safety.event_recorded:
                safety.event_recorded = True
                self.metrics.record_emergency(safety.source or "rule")
                await self._audit(AuditAction.EMERGENCY_DETECTED)
                await self._ticket("emergency", priority="urgent")
            return
        if handoff.reason == "unavailable":
            # The reply could not be finished; the team calls the person back.
            await self._ticket("unavailable")
            return
        if safety.transfer_attempted:
            return
        # The caller has agreed by now (see app.safety.handover).
        targets = (
            handover.targets_now(self.session.configuration, self.state)
            if safety.decision == SafetyDecision.TRANSFER
            else []
        )
        target = handover.choose(targets, safety.transfer_target)
        safety.transfer_attempted = True
        await self._audit(AuditAction.HUMAN_TRANSFER_REQUESTED)
        # A ticket either way: if the transfer fails, or nobody picks up, the request
        # is still waiting for the team.
        await self._ticket(handoff.reason)
        if target is None:
            # A message for the team was taken; the conversation carries on.
            self.metrics.record_human_transfer("unavailable")
            self._carry_on()
            return
        safety.transfer_target = target.name
        hooks = (
            f"{self.settings.telephony_public_base_url}/webhooks/v1/twilio/"
            f"{self.session.tenant_id}/{self.session.conversation_id}"
        )
        try:
            await transfer_call(
                self.settings,
                self.session.call,
                target.number,
                credentials=self.account,
                whisper_url=hooks + "/whisper",
                action_url=hooks + "/transfer-result",
            )
        except TransferError:
            self.metrics.record_human_transfer("failed")
            self._carry_on()
            await pipeline.speak(transfer_failed_message(self.state.language))
            await self._audit(AuditAction.HUMAN_TRANSFER_FAILED)
            return
        safety.transfer_confirmed = True
        self.transferred = True
        self.metrics.record_human_transfer("redirected")
        await self._audit(AuditAction.HUMAN_TRANSFER_REDIRECTED)
        with suppress(Exception):
            # Read back if staff do not pick up and the call returns here.
            await self.store.save(self.state, self.store_ttl_seconds)

    def _carry_on(self) -> None:
        """Nobody could be put through: the team has the request, the call goes on."""
        safety = self.state.safety_state
        safety.decision = SafetyDecision.CONTINUE
        safety.transfer_offered = False
        self.state.active_flow_state.reset()

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
                    description=(
                        self.state.safety_state.handoff_note
                        or self.state.last_caller_utterance
                        or ""
                    )[:500],
                    priority=priority,
                    patient_id=identity.patient_id if identity.is_verified else None,
                    contact=self.session.contact,
                    call_id=self.session.conversation_id,
                    dedupe_key=f"call:{self.session.conversation_id}:{reason}",
                    author="assistant",
                )
            self.tickets += 1
        except SQLAlchemyError:
            logger.error(
                "call_ticket_not_opened", extra={"call_id": str(self.session.conversation_id)}
            )
