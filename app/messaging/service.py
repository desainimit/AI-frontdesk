"""Text messages with patients: what comes in, the receptionist's replies, and the
staff member who takes a thread over.

Replies go out through the same queue as every other message to a patient.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.engine import Done, Handoff, Say
from app.ai.runtime import AssistantRuntime
from app.ai.session import Session
from app.ai.usage import record_turn
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, SafetyDecision
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import ApplicationError, NotFoundError
from app.languages import all_packs
from app.messaging.models import Conversation, ConversationMessage
from app.notifications.models import OutboundMessage
from app.notifications.repository import MessageRepository
from app.patients.models import Patient
from app.settings.store import load_settings
from app.tenants.models import Tenant
from app.tickets.service import open_ticket
from app.users.models import User

logger = logging.getLogger(__name__)

# The words carriers treat as "stop texting me" and "start again".
# Texts that stop and restart messages: the carriers' English words, which work in
# every language, and each language's own.
STOP_WORDS = frozenset[str]().union(*(item.stop_words for item in all_packs()))
START_WORDS = frozenset[str]().union(*(item.start_words for item in all_packs()))
# One turn may take this long before another process may pick the thread up.
LEASE = timedelta(seconds=90)
# After this long without a word the assistant starts the thread afresh.
MEMORY = timedelta(hours=12)
MAX_TEXT = 1500
HANDOFFS = {
    "requested": "A patient asked for a person by text",
    "emergency": "Possible emergency in a text message",
    "unavailable": "The receptionist could not finish a text conversation",
}


class CannotTextError(ApplicationError):
    code = "conversation_cannot_text"
    status_code = 409
    public_message = "This clinic has no phone number to send texts from."


class StaffText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=MAX_TEXT)


class ConversationView(BaseModel):
    id: UUID
    contact: str | None
    patient_id: UUID | None
    patient_name: str | None
    handler: str
    assigned_user_id: UUID | None
    assigned_name: str | None
    last_message_at: datetime
    last_preview: str
    unread: int
    # The assistant has not answered the latest message yet.
    waiting: bool


class ThreadMessage(BaseModel):
    id: UUID
    author: str
    author_name: str | None
    body: str
    # For a message we sent: queued, sent, failed.
    delivery: str | None
    created_at: datetime


class ConversationDetail(ConversationView):
    messages: list[ThreadMessage]
    can_text: bool


@dataclass(frozen=True)
class Received:
    conversation_id: UUID
    # Whether the receptionist should now answer.
    answer: bool


def _system(tenant_id: UUID) -> RequestContext:
    return RequestContext(uuid4(), tenant_id, None, ActorType.SYSTEM)


class InboundTexts:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def receive(
        self, tenant_id: UUID, clinic_number: str, sender: str, body: str, sid: str
    ) -> Received:
        """Keep an incoming text once, however often the phone service delivers it."""
        word = body.strip().upper()
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, tenant_id)
            if word in STOP_WORDS or word in START_WORDS:
                await session.execute(
                    update(Patient)
                    .where(Patient.tenant_id == tenant_id, Patient.phone == sender)
                    .values(sms_opt_out=word in STOP_WORDS)
                )
                if word in STOP_WORDS:
                    await MessageRepository(session, _system(tenant_id)).cancel_texts_to(sender)
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.channel == "sms",
                    Conversation.contact == sender,
                )
                .with_for_update()
            )
            if conversation is None:
                known = (
                    await session.scalars(
                        select(Patient.id)
                        .where(Patient.tenant_id == tenant_id, Patient.phone == sender)
                        .limit(2)
                    )
                ).all()
                conversation = Conversation(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    channel="sms",
                    contact=sender,
                    clinic_number=clinic_number,
                    patient_id=known[0] if len(known) == 1 else None,
                )
                session.add(conversation)
                await session.flush()
            stored = (
                await session.execute(
                    insert(ConversationMessage)
                    .values(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        conversation_id=conversation.id,
                        author="patient",
                        body=body[:MAX_TEXT],
                        provider_message_id=sid,
                    )
                    .on_conflict_do_nothing(constraint="uq_conversation_messages_sid")
                    .returning(ConversationMessage.created_at)
                )
            ).first()
            if stored is None:
                return Received(conversation.id, False)
            # The database's own clock, so "waiting since" lines up with the messages.
            now: datetime = stored[0]
            conversation.status = "open"
            conversation.clinic_number = clinic_number
            conversation.last_message_at = now
            conversation.last_preview = body[:160]
            conversation.unread += 1
            live = await session.scalar(
                select(Tenant.id).where(
                    Tenant.id == tenant_id,
                    Tenant.is_active.is_(True),
                    Tenant.service_state == "live",
                )
            )
            answer = (
                conversation.handler == "assistant"
                and live is not None
                and word not in STOP_WORDS
                and word not in START_WORDS
            )
            if answer and conversation.pending_since is None:
                conversation.pending_since = now
            return Received(conversation.id, answer)


class TextAssistant:
    """Answers what is waiting in a thread, one turn at a time per thread."""

    def __init__(self, runtime: AssistantRuntime) -> None:
        self._runtime = runtime
        self._sessions = runtime.sessions

    async def answer(self, tenant_id: UUID, conversation_id: UUID) -> bool:
        """Reply to the patient's unanswered messages. True when more arrived meanwhile."""
        claim = await self._claim(tenant_id, conversation_id)
        if claim is None:
            return False
        session, state, said, last_at = claim
        reply: list[str] = []
        handoff: Handoff | None = None
        done: Done | None = None
        try:
            ConversationStateManager.process_caller_utterance(state, said)
            async with self._runtime.open(tenant_id) as engine:
                async for event in engine.run(session, state, said):
                    if isinstance(event, Say):
                        reply.append(event.text)
                    elif isinstance(event, Handoff):
                        handoff = event
                    elif isinstance(event, Done):
                        done = event
        except Exception:
            # Whatever went wrong, the patient is not left without an answer.
            logger.error("text_turn_failed", extra={"conversation_id": str(conversation_id)})
            reply = [reply[0]] if reply else []
            handoff = Handoff("unavailable")
        body = " ".join(reply)[:MAX_TEXT]
        if body:
            ConversationStateManager.record_agent_response(state, body)
            state.append_transcript_turn("agent", body)
        more = await self._finish(session, state, body, handoff, said, last_at)
        if done is not None:
            await record_turn(self._sessions, session, done)
        return more

    async def _claim(
        self, tenant_id: UUID, conversation_id: UUID
    ) -> tuple[Session, ConversationState, str, datetime] | None:
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, tenant_id)
            conversation = await session.scalar(
                select(Conversation)
                .where(Conversation.tenant_id == tenant_id, Conversation.id == conversation_id)
                .with_for_update(skip_locked=True)
            )
            if (
                conversation is None
                or conversation.pending_since is None
                or conversation.handler != "assistant"
                or (conversation.lease_until is not None and conversation.lease_until > now)
            ):
                return None
            waiting = (
                await session.execute(
                    select(ConversationMessage.body, ConversationMessage.created_at)
                    .where(
                        ConversationMessage.tenant_id == tenant_id,
                        ConversationMessage.conversation_id == conversation_id,
                        ConversationMessage.author == "patient",
                        ConversationMessage.created_at >= conversation.pending_since,
                    )
                    .order_by(ConversationMessage.created_at)
                )
            ).all()
            if not waiting:
                conversation.pending_since = None
                return None
            conversation.lease_until = now + LEASE
            configuration = await load_settings(session, tenant_id)
            state = self._memory(conversation, now)
            if state is None:
                state = ConversationStateManager.create_initial_state(
                    call_id=conversation.id,
                    tenant_id=tenant_id,
                    telephony_call_id="text",
                    caller_phone=conversation.contact,
                    language_policy=configuration.receptionist.languages,
                )
            assistant = Session(
                tenant_id,
                conversation.id,
                "sms" if conversation.channel == "sms" else "test",
                uuid4(),
                configuration,
                configuration.receptionist.allowed_tools,
                contact=conversation.contact,
            )
            # Several texts sent in a row are read as one message.
            said = "\n".join(row.body for row in waiting)[:MAX_TEXT]
            return assistant, state, said, waiting[-1].created_at

    @staticmethod
    def _memory(conversation: Conversation, now: datetime) -> ConversationState | None:
        """What the assistant remembers of the thread, unless it is time to start afresh."""
        if conversation.state is None:
            return None
        try:
            state = ConversationState.model_validate(conversation.state)
        except ValidationError:
            return None
        if (
            now - state.updated_at > MEMORY
            or state.safety_state.decision != SafetyDecision.CONTINUE
        ):
            return None
        return state

    async def _finish(
        self,
        assistant: Session,
        state: ConversationState,
        body: str,
        handoff: Handoff | None,
        said: str,
        last_at: datetime,
    ) -> bool:
        tenant_id, now = assistant.tenant_id, datetime.now(UTC)
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, tenant_id)
            conversation = await session.scalar(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == assistant.conversation_id,
                )
                .with_for_update()
            )
            if conversation is None:
                return False
            if body:
                await _send(session, conversation, body, author="assistant")
            conversation.state = state.model_dump(mode="json")
            conversation.lease_until = None
            newer = await session.scalar(
                select(func.min(ConversationMessage.created_at)).where(
                    ConversationMessage.tenant_id == tenant_id,
                    ConversationMessage.conversation_id == conversation.id,
                    ConversationMessage.author == "patient",
                    ConversationMessage.created_at > last_at,
                )
            )
            conversation.pending_since = newer
            if handoff is not None:
                # A person answers from here; the thread waits for them.
                conversation.handler = "staff"
                conversation.pending_since = None
                await open_ticket(
                    session,
                    tenant_id,
                    source="sms",
                    subject=HANDOFFS[handoff.reason],
                    description=said[:500],
                    priority="urgent" if handoff.reason == "emergency" else "high",
                    patient_id=conversation.patient_id,
                    contact=conversation.contact,
                    conversation_id=conversation.id,
                    dedupe_key=f"conversation:{conversation.id}:{now:%Y%m%d%H}",
                    author="assistant",
                )
            return conversation.pending_since is not None

    async def backlog(self, limit: int = 20) -> Sequence[tuple[UUID, UUID]]:
        """Threads whose turn was lost, for instance when the server restarted mid-reply."""
        cutoff = datetime.now(UTC) - timedelta(seconds=30)
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            # Read across clinics for this one query only; see the table's policy.
            await session.execute(text("SELECT set_config('app.assistant_backlog', 'on', true)"))
            rows = await session.execute(
                select(Conversation.tenant_id, Conversation.id)
                .where(
                    Conversation.pending_since < cutoff,
                    Conversation.handler == "assistant",
                    or_(
                        Conversation.lease_until.is_(None),
                        Conversation.lease_until < func.now(),
                    ),
                )
                .order_by(Conversation.pending_since)
                .limit(limit)
            )
            return [(row[0], row[1]) for row in rows]


async def _send(
    session: AsyncSession,
    conversation: Conversation,
    body: str,
    *,
    author: Literal["assistant", "staff"],
    user_id: UUID | None = None,
) -> None:
    """Add our message to the thread and, for a real thread, queue the text."""
    message_id, outbound = uuid4(), None
    if conversation.channel == "sms" and conversation.contact:
        if conversation.clinic_number is None:
            raise CannotTextError()
        outbound = await MessageRepository(session, _system(conversation.tenant_id)).enqueue(
            channel="sms",
            kind="reply",
            recipient=conversation.contact,
            sender=conversation.clinic_number,
            body=body,
            dedupe_key=f"conversation:{message_id}",
        )
    session.add(
        ConversationMessage(
            id=message_id,
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            author=author,
            author_user_id=user_id,
            body=body,
            outbound_id=outbound,
        )
    )
    conversation.last_message_at = datetime.now(UTC)
    conversation.last_preview = body[:160]


class ConversationService:
    """What staff see and do in Messages."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        view: Literal["all", "unread", "mine", "staff"] = "all",
        search: str | None = None,
    ) -> Sequence[ConversationView]:
        require_permission(context, Permission.CALL_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            query = (
                select(Conversation, Patient.display_name, User.display_name)
                .outerjoin(Patient, Patient.id == Conversation.patient_id)
                .outerjoin(User, User.id == Conversation.assigned_user_id)
                .where(Conversation.tenant_id == context.tenant_id, Conversation.channel == "sms")
            )
            if view == "unread":
                query = query.where(Conversation.unread > 0)
            elif view == "mine":
                query = query.where(Conversation.assigned_user_id == context.actor_id)
            elif view == "staff":
                query = query.where(Conversation.handler == "staff")
            if search:
                needle = f"%{search.strip().lower()}%"
                query = query.where(
                    or_(
                        func.lower(Patient.display_name).like(needle),
                        Conversation.contact.like(needle),
                    )
                )
            rows = (
                await session.execute(
                    query.order_by(Conversation.last_message_at.desc(), Conversation.id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            return [_view(row[0], row[1], row[2]) for row in rows]

    async def unread(self, context: RequestContext) -> int:
        require_permission(context, Permission.CALL_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            return int(
                await session.scalar(
                    select(func.count()).where(
                        Conversation.tenant_id == context.tenant_id,
                        Conversation.channel == "sms",
                        Conversation.unread > 0,
                    )
                )
                or 0
            )

    async def get(self, context: RequestContext, identity: UUID) -> ConversationDetail:
        """The thread, oldest message first. Opening it marks it as read."""
        require_permission(context, Permission.CALL_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            conversation = await self._one(session, context, identity)
            conversation.unread = 0
            return await self._detail(session, conversation)

    async def reply(
        self, context: RequestContext, identity: UUID, command: StaffText
    ) -> ConversationDetail:
        """A staff member writes to the patient, which also takes the thread over."""
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            conversation = await self._one(session, context, identity)
            await _send(
                session, conversation, command.body, author="staff", user_id=context.actor_id
            )
            self._take(conversation, context)
            return await self._detail(session, conversation)

    async def take_over(self, context: RequestContext, identity: UUID) -> ConversationDetail:
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            conversation = await self._one(session, context, identity)
            self._take(conversation, context)
            return await self._detail(session, conversation)

    async def hand_back(self, context: RequestContext, identity: UUID) -> ConversationDetail:
        """Give the thread back to the receptionist, which starts it afresh."""
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            conversation = await self._one(session, context, identity)
            conversation.handler = "assistant"
            conversation.assigned_user_id = None
            conversation.state = None
            conversation.pending_since = None
            return await self._detail(session, conversation)

    @staticmethod
    def _take(conversation: Conversation, context: RequestContext) -> None:
        conversation.handler = "staff"
        conversation.assigned_user_id = context.actor_id
        conversation.pending_since = None
        conversation.unread = 0

    @staticmethod
    async def _one(session: AsyncSession, context: RequestContext, identity: UUID) -> Conversation:
        conversation = await session.scalar(
            select(Conversation)
            .where(
                Conversation.tenant_id == context.tenant_id,
                Conversation.id == identity,
                Conversation.channel == "sms",
            )
            .with_for_update()
        )
        if conversation is None:
            raise NotFoundError()
        return conversation

    async def _detail(
        self, session: AsyncSession, conversation: Conversation
    ) -> ConversationDetail:
        await session.flush()
        names: Any = (
            await session.execute(
                select(Patient.display_name, User.display_name)
                .select_from(Conversation)
                .outerjoin(Patient, Patient.id == Conversation.patient_id)
                .outerjoin(User, User.id == Conversation.assigned_user_id)
                .where(Conversation.id == conversation.id)
            )
        ).one()
        rows = (
            await session.execute(
                select(ConversationMessage, User.display_name, OutboundMessage.status)
                .outerjoin(User, User.id == ConversationMessage.author_user_id)
                .outerjoin(OutboundMessage, OutboundMessage.id == ConversationMessage.outbound_id)
                .where(
                    ConversationMessage.tenant_id == conversation.tenant_id,
                    ConversationMessage.conversation_id == conversation.id,
                )
                .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id)
                .limit(200)
            )
        ).all()
        return ConversationDetail(
            **_view(conversation, names[0], names[1]).model_dump(),
            messages=[
                ThreadMessage(
                    id=message.id,
                    author=message.author,
                    author_name=name,
                    body=message.body,
                    delivery=delivery,
                    created_at=message.created_at,
                )
                for message, name, delivery in reversed(rows)
            ],
            can_text=conversation.clinic_number is not None,
        )


def _view(
    conversation: Conversation, patient_name: str | None, assigned_name: str | None
) -> ConversationView:
    return ConversationView(
        id=conversation.id,
        contact=conversation.contact,
        patient_id=conversation.patient_id,
        patient_name=patient_name,
        handler=conversation.handler,
        assigned_user_id=conversation.assigned_user_id,
        assigned_name=assigned_name,
        last_message_at=conversation.last_message_at,
        last_preview=conversation.last_preview,
        unread=conversation.unread,
        waiting=conversation.pending_since is not None,
    )
