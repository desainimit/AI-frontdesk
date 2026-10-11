"""Try your receptionist: a staff member chats with the assistant exactly as a patient
would, and sees what it looked up. Nothing in a test chat changes clinic data."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select, update

from app.ai.engine import Done, Handoff, Say, Worked, Working
from app.ai.runtime import AssistantRuntime
from app.ai.session import Session
from app.ai.usage import record_turn
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, SafetyDecision
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.database.unit_of_work import enter_tenant_scope
from app.messaging.models import Conversation
from app.settings.store import load_settings

# What each tool is called on screen.
TOOL_LABELS = {
    "list_services": "Looked up appointment types",
    "list_locations": "Looked up locations",
    "list_providers": "Looked up doctors",
    "get_knowledge": "Searched the clinic's knowledge",
    "check_availability": "Checked free times",
    "list_my_appointments": "Looked up the patient's appointments",
    "book_appointment": "Proposed a booking",
    "cancel_appointment": "Proposed a cancellation",
    "reschedule_appointment": "Proposed a new time",
    "identify_patient": "Checked a reception code",
    "register_patient": "Registered a new patient",
    "create_ticket": "Took a message for the team",
    "transfer_to_staff": "Asked for a person",
}


class TestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    __test__ = False
    message: str = Field(min_length=1, max_length=1000)
    # Leave out to start a new chat; send back to continue one.
    conversation_id: UUID | None = None


@dataclass(frozen=True)
class Prepared:
    session: Session
    state: ConversationState


class TestChat:
    __test__ = False

    def __init__(self, runtime: AssistantRuntime) -> None:
        self._runtime = runtime
        self._sessions = runtime.sessions

    async def prepare(self, context: RequestContext, conversation_id: UUID | None) -> Prepared:
        """Find or start the chat. Fails here, before anything is streamed."""
        require_permission(context, Permission.SETTINGS_READ)
        async with self._sessions.begin() as database:
            await enter_tenant_scope(database, context)
            configuration = await load_settings(database, context.tenant_id)
            conversation = None
            if conversation_id is not None:
                conversation = await database.scalar(
                    select(Conversation).where(
                        Conversation.tenant_id == context.tenant_id,
                        Conversation.id == conversation_id,
                        Conversation.channel == "test",
                        Conversation.assigned_user_id == context.actor_id,
                    )
                )
            state: ConversationState | None = None
            if conversation is None:
                # One test chat per person: starting a new one clears the last.
                await database.execute(
                    delete(Conversation).where(
                        Conversation.tenant_id == context.tenant_id,
                        Conversation.channel == "test",
                        Conversation.assigned_user_id == context.actor_id,
                    )
                )
                conversation = Conversation(
                    id=uuid4(),
                    tenant_id=context.tenant_id,
                    channel="test",
                    assigned_user_id=context.actor_id,
                )
                database.add(conversation)
                await database.flush()
            elif conversation.state is not None:
                try:
                    state = ConversationState.model_validate(conversation.state)
                except ValidationError:
                    state = None
                if state is not None and state.safety_state.decision != SafetyDecision.CONTINUE:
                    # The last message ended in a hand-over; the next one starts afresh.
                    state = None
            if state is None:
                state = ConversationStateManager.create_initial_state(
                    call_id=conversation.id,
                    tenant_id=context.tenant_id,
                    telephony_call_id="test",
                    language_policy=configuration.receptionist.languages,
                )
            return Prepared(
                Session(
                    context.tenant_id,
                    conversation.id,
                    "test",
                    context.request_id,
                    configuration,
                    configuration.receptionist.allowed_tools,
                ),
                state,
            )

    async def run(
        self, prepared: Prepared, message: str
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """The turn as named events for the browser."""
        session, state = prepared.session, prepared.state
        yield "start", {"conversation_id": str(session.conversation_id)}
        ConversationStateManager.process_caller_utterance(state, message)
        reply: list[str] = []
        done: Done | None = None
        try:
            async with self._runtime.open(session.tenant_id) as engine:
                async for event in engine.run(session, state, message):
                    if isinstance(event, Say):
                        reply.append(event.text)
                        yield "say", {"text": event.text}
                    elif isinstance(event, Working):
                        yield "working", {"label": TOOL_LABELS.get(event.tool, event.tool)}
                    elif isinstance(event, Worked):
                        yield (
                            "worked",
                            {"label": TOOL_LABELS.get(event.tool, event.tool), "ok": event.ok},
                        )
                    elif isinstance(event, Handoff):
                        yield "handoff", {"reason": event.reason}
                    else:
                        done = event
        finally:
            text = " ".join(reply)
            if text:
                ConversationStateManager.record_agent_response(state, text)
                state.append_transcript_turn("agent", text)
            async with self._sessions.begin() as database:
                await verify_runtime_role(database)
                await set_tenant_scope(database, session.tenant_id)
                await database.execute(
                    update(Conversation)
                    .where(
                        Conversation.tenant_id == session.tenant_id,
                        Conversation.id == session.conversation_id,
                    )
                    .values(state=state.model_dump(mode="json"))
                )
            if done is not None:
                await record_turn(self._sessions, session, done)
        if done is not None:
            yield (
                "done",
                {
                    "outcome": done.outcome,
                    "first_text_ms": done.first_text_ms,
                    "total_ms": done.total_ms,
                },
            )
