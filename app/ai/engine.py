"""One turn of the receptionist, the same on the phone, by text and in the test chat.

The engine yields what happens as it happens: sentences as soon as they are complete,
lookups as they start and finish, a hand-over when a person is needed, and at the end
what the turn cost. The channel decides what to do with each: speak it, send it, show it.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.ai.catalog import MUTATION_TOOLS
from app.ai.consent import ConsentReader
from app.ai.identity import ContactIdentity
from app.ai.llm.base import (
    ChatRequest,
    Completed,
    LLMBlockedError,
    LLMInterruptedError,
    LLMInvalidResponseError,
    LLMProviderError,
    ModelEvent,
    TextDelta,
    ToolCall,
    ToolResult,
    Turn,
    Usage,
    UsageMeter,
    UserTurn,
)
from app.ai.memory import CallerHistory, records_trusted
from app.ai.prompts import history, language_name, prompt_parts
from app.ai.sentences import SentenceSplitter
from app.ai.session import AssistantAuthorizationError, Session
from app.ai.tools.appointments import AppointmentFlow
from app.ai.tools.registry import ToolRegistry, ToolValidationError
from app.ai.wording import spoken
from app.conversation.confirmation import confirmation_answer
from app.conversation.language import CHECK_WORDS, reply_fits
from app.conversation.manager import ConversationStateManager
from app.conversation.promises import promises_a_lookup, settles_a_time
from app.conversation.redaction import redact_verification_secrets
from app.conversation.state import ConversationState, SafetyDecision
from app.core.errors import ApplicationError
from app.safety import handover
from app.safety.engine import SafetyEngine
from app.safety.handover_messages import handover_message
from app.safety.numbers import emergency_lines

logger = logging.getLogger(__name__)

# Their results concern who the person is; they are not repeated in later turns.
UNREMEMBERED_TOOLS = frozenset({"identify_patient", "confirm_identity", "register_patient"})
MAX_STEPS = 4
MAX_TOOL_CALLS = 6
TURN_SECONDS = 25.0
# How long the first words wait for the model's safety reading of what was said. None:
# the reply starts at once, and an alarm raised later cuts in (see `Say.urgent`).
SAFETY_WAIT_SECONDS = 0.0
# Not the caller: the server reminding the model of a lookup it said it would make.
KEEP_PROMISE = (
    "(Server note, not the caller.) You told the caller you would check. Make that "
    "lookup now with a tool, without saying anything first."
)

# Not the caller: the server asking for a reply in the call's language.
RIGHT_LANGUAGE = (
    "(Server note, not the caller.) That reply was not in {language}, the language of "
    "this call. Give your reply again, in {language} only."
)
# Not the caller: the server, when a reply spoke of an offered time as settled.
BOOK_IT = (
    "(Server note, not the caller.) You spoke of {time} as settled, but nothing is booked "
    "until book_appointment and their yes. If they chose it, call book_appointment now "
    "(register_patient first if person_is_known is false) without saying anything first. "
    "If they did not choose it, say nothing."
)

Outcome = Literal[
    "answered", "confirmation", "safety", "handoff", "unavailable", "blocked", "interrupted"
]


# A plain "no, thanks" is answered with a fixed line; anything longer is a new request.
PLAIN_NO_WORDS = 4


def _says_more_than_no(words: str) -> bool:
    return "?" in words or len(words.split()) > PLAIN_NO_WORDS


class LanguageGate:
    """Holds the first words of a reply until there are enough to tell their language.

    A reply in another language than the call's is then not said at all (27 Oct: an
    English line in a Hindi call); `wrong` is set and the model is asked again.
    """

    def __init__(self, language: str, *, check: bool) -> None:
        self._language = language
        self._checking = check
        self._held: list[str] = []
        self.wrong = False

    def feed(self, sentence: str) -> list[str]:
        """What can be said now."""
        if self.wrong:
            return []
        if not self._checking:
            return [sentence]
        self._held.append(sentence)
        if len(" ".join(self._held).split()) < CHECK_WORDS:
            return []
        return self._decide()

    def finish(self, rest: str) -> list[str]:
        """The end of the reply: whatever is still held, checked if it was not yet."""
        if rest:
            self._held.append(rest)
        if self.wrong or not self._checking:
            return self._held if not self.wrong else []
        return self._decide()

    def _decide(self) -> list[str]:
        self._checking = False
        if not reply_fits(" ".join(self._held), self._language):
            self.wrong = True
            return []
        ready, self._held = self._held, []
        return ready


def _reminder(state: ConversationState, completed: Completed) -> str | None:
    """A server note for the model when its reply left something undone, or None."""
    text = completed.turn.text
    if completed.turn.tool_calls:
        return None
    # "Let me check" and nothing more would leave the caller in silence. A question
    # back ("...may I have your name?") waits for the caller.
    if promises_a_lookup(text) and not text.rstrip().endswith("?"):
        return KEEP_PROMISE
    # A time spoken of as theirs, but nothing is booked until book_appointment and
    # their yes (27 Oct: "पक्का, सवा नौ बजे का रख लेते हैं" and no booking).
    if state.appointment_operation is None:
        settled = settles_a_time(text, state.active_flow_state.offered_slots)
        if settled is not None:
            return BOOK_IT.format(time=settled)
    return None


async def _close(stream: AsyncIterator[Any]) -> None:
    """Stop a model reply that is not wanted, so its request ends too."""
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()


@dataclass(frozen=True)
class Say:
    """The next piece of the reply, complete enough to speak or send."""

    text: str
    # Stop whatever is still being said and say this instead (a safety alarm).
    urgent: bool = False


@dataclass(frozen=True)
class Working:
    tool: str
    # What the lookup is for (the doctor, the day), so the call can say so aloud.
    arguments: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class Worked:
    tool: str
    ok: bool


@dataclass(frozen=True)
class Handoff:
    """A person has to take it from here."""

    reason: Literal["requested", "emergency", "unavailable"]


@dataclass(frozen=True)
class Done:
    outcome: Outcome
    reply: str
    tools: tuple[str, ...] = ()
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    first_text_ms: int | None = None
    total_ms: int = 0
    steps: int = 0
    error: str | None = None
    # What the quick checks beside this reply used (safety, yes/no, summary).
    checks: Usage = field(default_factory=Usage)
    # Where the reply was served, when the provider says.
    served_by: str = ""


Event = Say | Working | Worked | Handoff | Done


class Model(Protocol):
    def stream(self, request: ChatRequest) -> AsyncIterator[ModelEvent]: ...


class Engine:
    def __init__(
        self,
        model: Model,
        registry: ToolRegistry,
        flow: AppointmentFlow,
        safety: SafetyEngine,
        identity: ContactIdentity,
        history: CallerHistory | None = None,
        consent: ConsentReader | None = None,
        *,
        meter: UsageMeter | None = None,
    ) -> None:
        self.model = model
        self.registry = registry
        self.flow = flow
        self.safety = safety
        self.identity = identity
        # Earlier calls and coming appointments, for a caller whose identity is trusted.
        self.history = history
        # Reads a yes/no the rules could not, while a question to the caller is open.
        self.consent = consent
        # Collects what the quick checks used, to be kept with the reply.
        self.meter = meter

    async def run(
        self, session: Session, state: ConversationState, utterance: str
    ) -> AsyncIterator[Event]:
        """Reply to what the person just said. The channel has already put their words
        into the state's transcript."""
        started = perf_counter()
        said: list[str] = []
        tools: list[str] = []
        usage, model, steps, served_by = Usage(), "", 0, ""
        first_text_ms: int | None = None
        semantic: asyncio.Task[str | None] | None = None

        def say(text: str, *, urgent: bool = False) -> Say:
            nonlocal first_text_ms
            if first_text_ms is None:
                first_text_ms = int((perf_counter() - started) * 1000)
            said.append(text)
            return Say(text, urgent)

        def done(outcome: Outcome, error: str | None = None) -> Done:
            return Done(
                outcome,
                " ".join(said),
                tuple(tools),
                usage,
                model,
                first_text_ms,
                int((perf_counter() - started) * 1000),
                steps,
                error,
                self.meter.take() if self.meter is not None else Usage(),
                served_by,
            )

        def handoff() -> Handoff | None:
            decision = state.safety_state.decision
            if decision == SafetyDecision.EMERGENCY_PROTOCOL:
                return Handoff("emergency")
            if decision in {SafetyDecision.TRANSFER, SafetyDecision.ESCALATE}:
                return Handoff("requested")
            return None

        try:
            session.validate_binding(state)
            if not utterance.strip() or len(utterance) > 4000:
                raise ToolValidationError("Invalid utterance size")
            words = redact_verification_secrets(utterance)
            answer = await self._answer(state, words)
            clinic = session.configuration.clinic
            self.safety.lines = emergency_lines(clinic.phone, clinic.timezone)
            if self.safety.ask_first:
                # On a call: who can take it right now (opening hours, on-call lines).
                self.safety.transfer_available = bool(
                    handover.targets_now(session.configuration, state)
                )
                # 0. The answer to "shall I put you through?".
                if handover.pending(state):
                    agreed = handover.consent(words, answer)
                    if agreed is True:
                        handover.agree(state, can_transfer=self.safety.transfer_available)
                        yield say(self._handoff_line(state))
                        yield Handoff("requested")
                        yield done("handoff")
                        return
                    if agreed is False:
                        handover.withdraw(state)
                        if not _says_more_than_no(words):
                            yield say(handover_message(state.language, "declined"))
                            yield done("answered")
                            return
                        # "No, can you transfer the call?": the no is to the offer; what
                        # they ask next goes to the model below.
                elif state.safety_state.transfer_offered:
                    handover.withdraw(state)
            # 1. The instant safety rules. They need no model and nothing outranks them.
            fixed = await self.safety.evaluate(state, words, semantic=False)
            if fixed is not None:
                yield say(fixed)
                if self._offer_after_emergency(state):
                    yield say(handover_message(state.language, "after_emergency"))
                if (event := handoff()) is not None:
                    yield event
                yield done("safety")
                return
            session.validate(state)
            # 2. Who they are, and what is known about them.
            await self._know_the_caller(session, state)
            # 3. What the server answers itself: a code, or yes/no to its own question.
            direct = await self.flow.before_turn(session, state, utterance, answer)
            if direct is not None:
                tools.append(direct.tool)
                yield say(direct.response)
                yield done("confirmation")
                return
            # 4. The model, with its own reading of the risk running beside it.
            if self.safety.classifier is not None:
                semantic = asyncio.create_task(self.safety.semantic_check(state, words))
            turns: list[Turn] = history(state, words)
            specs = self.registry.specs(session)
            system, context = prompt_parts(session, state)
            conversation = str(session.conversation_id)
            reminded = False
            relanguaged = False
            while steps < MAX_STEPS:
                if perf_counter() - started > TURN_SECONDS:
                    raise TimeoutError
                steps += 1
                # On a call the first words are spoken while the rest is written.
                splitter = SentenceSplitter(first_clause=session.channel == "voice")
                gate = LanguageGate(state.language, check=not relanguaged)
                completed: Completed | None = None
                stream = self.model.stream(
                    ChatRequest(system, tuple(turns), specs, context, conversation)
                )
                try:
                    async for item in stream:
                        if not isinstance(item, TextDelta):
                            completed = item
                            continue
                        for sentence in splitter.feed(item.text):
                            semantic, alarm = await self._alarm(semantic, started)
                            if alarm is not None:
                                yield say(alarm, urgent=bool(said))
                                if (event := handoff()) is not None:
                                    yield event
                                yield done("safety")
                                return
                            for line in gate.feed(sentence):
                                yield say(line)
                        if gate.wrong:
                            break
                except LLMInvalidResponseError:
                    if not reminded:
                        raise
                    # Reminded of a lookup, it had nothing to look up: what it
                    # already said stands.
                    yield done("answered")
                    return
                if gate.wrong:
                    await _close(stream)
                if completed is None and not gate.wrong:
                    raise LLMInvalidResponseError("LLM returned no usable response")
                if completed is not None:
                    usage = usage + completed.usage
                    model = completed.model or model
                    served_by = completed.served_by or served_by
                    semantic, alarm = await self._alarm(semantic, started)
                    if alarm is not None:
                        yield say(alarm, urgent=bool(said))
                        if (event := handoff()) is not None:
                            yield event
                        yield done("safety")
                        return
                    ready = gate.finish(splitter.finish(cut_off=completed.finish == "length"))
                    for line in ready:
                        yield say(line)
                if gate.wrong or completed is None:
                    # Nothing of it was said; asked for again in the call's language, once.
                    relanguaged = True
                    note = RIGHT_LANGUAGE.format(language=language_name(state.language))
                    turns.append(UserTurn(note))
                    continue
                calls = completed.turn.tool_calls
                note = None if reminded or steps >= MAX_STEPS else _reminder(state, completed)
                if note is not None:
                    reminded = True
                    turns += [completed.turn, UserTurn(note)]
                    continue
                if not calls:
                    yield done("answered")
                    return
                if len(tools) + len(calls) > MAX_TOOL_CALLS:
                    raise ToolValidationError("Tool budget exhausted")
                turns.append(completed.turn)
                proposals = [call for call in calls if call.name in MUTATION_TOOLS]
                # Lookups and registering first, the proposal last: its question to the
                # person ends the turn, and it needs to know who they are ("my name is
                # Asha" comes as register_patient and book_appointment together).
                ordered = [call for call in calls if call.name not in MUTATION_TOOLS] + proposals
                for call in ordered:
                    yield Working(call.name, call.arguments or {})
                    result, ok = await self._execute(session, state, call, len(proposals))
                    tools.append(call.name)
                    ConversationStateManager.record_tool_call(state, call.name)
                    if call.name not in UNREMEMBERED_TOOLS:
                        state.record_lookup(call.name, result)
                    yield Worked(call.name, ok)
                    turns.append(ToolResult(call.id, call.name, result))
                    if (event := handoff()) is not None:
                        # The tool asked for a person.
                        yield say(self._handoff_line(state))
                        yield event
                        yield done("handoff")
                        return
                    spoken_by_server = str(result.get("say") or "")
                    if spoken_by_server:
                        # The server's own wording, such as the question to confirm.
                        yield say(spoken_by_server)
                        yield done("confirmation")
                        return
                    if call.name == "get_knowledge" and result.get("status") == "not_found":
                        configured = session.configuration
                        yield say(
                            configured.receptionist.unknown_answer_in(
                                state.language, configured.clinic.name
                            )
                        )
                        yield done("answered")
                        return
            raise ToolValidationError("Step budget exhausted")
        except AssistantAuthorizationError:
            yield say(spoken(state.language, "unavailable"))
            yield done("blocked", "not_authorized")
        except (LLMBlockedError, LLMInvalidResponseError) as error:
            # Nothing usable came back. Say so and offer a person; do not guess.
            if self.safety.ask_first:
                yield say(self._offer_help(state, "could_not_answer"))
            else:
                yield say(spoken(state.language, "could_not_answer"))
            yield done("blocked", error.code)
        except LLMInterruptedError as error:
            # Part of the reply was already delivered; it is never said twice.
            yield say(spoken(state.language, "say_again"))
            yield done("interrupted", error.code)
        except (LLMProviderError, TimeoutError, ToolValidationError, ValidationError) as error:
            code = error.code if isinstance(error, LLMProviderError) else "could_not_finish"
            logger.warning(
                "assistant_turn_unavailable",
                extra={"conversation_id": str(session.conversation_id), "error_type": code},
            )
            if self.safety.ask_first and not state.safety_state.transfer_attempted:
                # On a call, the caller decides: put through, or a message for the team.
                yield say(self._offer_help(state, "trouble"))
                yield done("unavailable", code)
                return
            yield say(
                spoken(
                    state.language,
                    "staff_will_call" if session.channel == "voice" else "staff_will_reply",
                )
            )
            yield Handoff("unavailable")
            yield done("unavailable", code)
        finally:
            if semantic is not None and not semantic.done():
                semantic.cancel()

    async def _know_the_caller(self, session: Session, state: ConversationState) -> None:
        """A number that belongs to one patient identifies them; then their name and,
        when their records may be used, what is known from earlier calls."""
        if not state.identity_state.is_verified and session.contact and session.writes:
            try:
                await self.identity.recognise(session, state)
            except ApplicationError:
                logger.warning("assistant_recognition_failed")
        if self.history is None:
            return
        if state.caller_name is None and state.identity_state.is_verified:
            try:
                state.caller_name = await self.history.name(session, state) or ""
            except (ApplicationError, SQLAlchemyError):
                logger.warning("assistant_name_unavailable")
                state.caller_name = ""
        if state.caller_history is None and records_trusted(session, state):
            try:
                state.caller_history = await self.history.load(session, state)
            except (ApplicationError, SQLAlchemyError):
                logger.warning("assistant_history_unavailable")
                state.caller_history = ""

    async def _execute(
        self, session: Session, state: ConversationState, call: ToolCall, proposals: int
    ) -> tuple[dict[str, Any], bool]:
        """One tool call's result for the model, and whether it worked. A failure is told
        to the model, which can try again; only a refusal to act ends the turn."""
        try:
            if call.name in MUTATION_TOOLS and proposals > 1:
                raise ToolValidationError("Propose one change at a time")
            return await self.registry.execute(session, state, call), True
        except AssistantAuthorizationError:
            raise
        except (ToolValidationError, ValidationError, ValueError):
            # The model can read this and try again with better arguments.
            return {"status": "invalid_arguments"}, False
        except (TimeoutError, ApplicationError) as error:
            code = error.code if isinstance(error, ApplicationError) else "timeout"
            return {"status": "failed", "reason": code}, False

    async def _answer(self, state: ConversationState, words: str) -> bool | None:
        """The reply read as yes or no, when the caller was asked something."""
        operation = state.appointment_operation
        booking = operation is not None and operation.status == "proposed"
        naming = state.identity_state.pending_name is not None
        if not booking and not naming and not (self.safety.ask_first and handover.pending(state)):
            return None
        answer = confirmation_answer(words, state.language)
        if answer is None and self.consent is not None:
            answer = await self.consent.read(state.last_agent_response or "", words)
        return answer

    def _handoff_line(self, state: ConversationState) -> str:
        if not self.safety.ask_first:
            return self.safety.message(state)
        decision = state.safety_state.decision
        key = "connecting" if decision == SafetyDecision.TRANSFER else "message_taken"
        return handover_message(state.language, key)

    def _offer_help(self, state: ConversationState, reason: str) -> str:
        """Something went wrong on this side: offer a person, or a message for the team."""
        handover.offer(state, None, state.last_caller_utterance)
        available = self.safety.transfer_available
        if reason == "could_not_answer" and not available:
            return spoken(state.language, "could_not_answer")
        return handover_message(state.language, "trouble_offer" if available else "trouble_message")

    def _offer_after_emergency(self, state: ConversationState) -> bool:
        """After the emergency script, once, the caller may also be put through."""
        safety = state.safety_state
        if not (
            self.safety.ask_first
            and self.safety.transfer_available
            and safety.decision == SafetyDecision.EMERGENCY_PROTOCOL
            and not safety.event_recorded
            and not safety.transfer_offered
        ):
            return False
        handover.offer(state, None, "Possible emergency")
        return True

    @staticmethod
    async def _alarm(
        task: asyncio.Task[str | None] | None, started: float
    ) -> tuple[asyncio.Task[str | None] | None, str | None]:
        """The safety reply if the model's reading raised the alarm.

        The first words wait a moment for it; after that it is checked without waiting.
        """
        if task is None:
            return None, None
        if not task.done():
            remaining = SAFETY_WAIT_SECONDS - (perf_counter() - started)
            if remaining > 0:
                await asyncio.wait({task}, timeout=remaining)
        if not task.done():
            return task, None
        try:
            return None, task.result()
        except Exception:
            # The rules already ran; a failed second opinion changes nothing.
            return None, None
