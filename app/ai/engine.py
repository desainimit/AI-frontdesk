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
    ToolResult,
    Turn,
    Usage,
    UserTurn,
)
from app.ai.memory import CallerHistory, records_trusted
from app.ai.prompts import history, system_prompt
from app.ai.sentences import SentenceSplitter
from app.ai.session import AssistantAuthorizationError, Session
from app.ai.tools.appointments import AppointmentFlow
from app.ai.tools.registry import ToolRegistry, ToolValidationError
from app.ai.wording import spoken
from app.conversation.confirmation import confirmation_answer
from app.conversation.manager import ConversationStateManager
from app.conversation.promises import promises_a_lookup
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

Outcome = Literal[
    "answered", "confirmation", "safety", "handoff", "unavailable", "blocked", "interrupted"
]


# A plain "no, thanks" is answered with a fixed line; anything longer is a new request.
PLAIN_NO_WORDS = 4


def _says_more_than_no(words: str) -> bool:
    return "?" in words or len(words.split()) > PLAIN_NO_WORDS


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

    async def run(
        self, session: Session, state: ConversationState, utterance: str
    ) -> AsyncIterator[Event]:
        """Reply to what the person just said. The channel has already put their words
        into the state's transcript."""
        started = perf_counter()
        said: list[str] = []
        tools: list[str] = []
        usage, model, steps = Usage(), "", 0
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
            # 2. A number that belongs to one patient identifies them.
            if not state.identity_state.is_verified and session.contact and session.writes:
                try:
                    await self.identity.recognise(session, state)
                except ApplicationError:
                    logger.warning("assistant_recognition_failed")
            if (
                self.history is not None
                and state.caller_name is None
                and state.identity_state.is_verified
            ):
                try:
                    state.caller_name = await self.history.name(session, state) or ""
                except (ApplicationError, SQLAlchemyError):
                    logger.warning("assistant_name_unavailable")
                    state.caller_name = ""
            if (
                self.history is not None
                and state.caller_history is None
                and records_trusted(session, state)
            ):
                try:
                    state.caller_history = await self.history.load(session, state)
                except (ApplicationError, SQLAlchemyError):
                    logger.warning("assistant_history_unavailable")
                    state.caller_history = ""
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
            system = system_prompt(session, state)
            reminded = False
            while steps < MAX_STEPS:
                if perf_counter() - started > TURN_SECONDS:
                    raise TimeoutError
                steps += 1
                # On a call the first words are spoken while the rest is written.
                splitter = SentenceSplitter(first_clause=session.channel == "voice")
                completed: Completed | None = None
                try:
                    async for item in self.model.stream(ChatRequest(system, tuple(turns), specs)):
                        if isinstance(item, TextDelta):
                            for sentence in splitter.feed(item.text):
                                semantic, alarm = await self._alarm(semantic, started)
                                if alarm is not None:
                                    yield say(alarm, urgent=bool(said))
                                    if (event := handoff()) is not None:
                                        yield event
                                    yield done("safety")
                                    return
                                yield say(sentence)
                        else:
                            completed = item
                except LLMInvalidResponseError:
                    if not reminded:
                        raise
                    # Reminded of a lookup, it had nothing to look up: what it
                    # already said stands.
                    yield done("answered")
                    return
                if completed is None:
                    raise LLMInvalidResponseError("LLM returned no usable response")
                usage = Usage(
                    usage.input_tokens + completed.usage.input_tokens,
                    usage.output_tokens + completed.usage.output_tokens,
                )
                model = completed.model or model
                semantic, alarm = await self._alarm(semantic, started)
                if alarm is not None:
                    yield say(alarm, urgent=bool(said))
                    if (event := handoff()) is not None:
                        yield event
                    yield done("safety")
                    return
                rest = splitter.finish(cut_off=completed.finish == "length")
                if rest:
                    yield say(rest)
                calls = completed.turn.tool_calls
                if (
                    not calls
                    # Even after a lookup: "Let me check Wednesday instead." and nothing
                    # more would leave the caller in silence.
                    and not reminded
                    and steps < MAX_STEPS
                    and promises_a_lookup(completed.turn.text)
                    # A question back ("...may I have your name?") waits for the caller.
                    and not completed.turn.text.rstrip().endswith("?")
                ):
                    # It said "let me check" and stopped: the caller would hear
                    # silence. Have it make the lookup it promised.
                    reminded = True
                    turns += [completed.turn, UserTurn(KEEP_PROMISE)]
                    continue
                if not calls:
                    yield done("answered")
                    return
                if len(tools) + len(calls) > MAX_TOOL_CALLS:
                    raise ToolValidationError("Tool budget exhausted")
                turns.append(completed.turn)
                proposals = [call for call in calls if call.name in MUTATION_TOOLS]
                for call in calls:
                    yield Working(call.name, call.arguments or {})
                    result: dict[str, Any]
                    try:
                        if proposals and (len(calls) > 1):
                            raise ToolValidationError("Propose one change at a time")
                        result = await self.registry.execute(session, state, call)
                        ok = True
                    except AssistantAuthorizationError:
                        raise
                    except (ToolValidationError, ValidationError, ValueError):
                        # The model can read this and try again with better arguments.
                        result, ok = {"status": "invalid_arguments"}, False
                    except (TimeoutError, ApplicationError) as error:
                        code = error.code if isinstance(error, ApplicationError) else "timeout"
                        result, ok = {"status": "failed", "reason": code}, False
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
                        yield say(session.configuration.receptionist.unknown_answer)
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

    async def _answer(self, state: ConversationState, words: str) -> bool | None:
        """The reply read as yes or no, when the caller was asked something."""
        operation = state.appointment_operation
        booking = operation is not None and operation.status == "proposed"
        if not booking and not (self.safety.ask_first and handover.pending(state)):
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
