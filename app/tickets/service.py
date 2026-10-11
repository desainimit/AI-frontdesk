"""Tickets: a patient's request that a staff member has to deal with.

Staff open them by hand; the assistant opens one when it takes a message or cannot
finish; the worker opens one for a call that needs a person or an appointment that was
changed in a provider's own calendar.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import String, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import ApplicationError, NotFoundError
from app.notifications.models import OutboundMessage
from app.notifications.repository import MessageRepository
from app.patients.models import Patient
from app.tickets.models import Ticket, TicketEvent
from app.users.models import User

Status = Literal["open", "in_progress", "waiting", "resolved"]
Priority = Literal["low", "normal", "high", "urgent"]
STATUS_LABELS = {
    "open": "Open",
    "in_progress": "In progress",
    "waiting": "Waiting on patient",
    "resolved": "Resolved",
}


class NoWayToReplyError(ApplicationError):
    code = "ticket_no_contact"
    status_code = 409
    public_message = "There is no phone number or email to reply to on this ticket."


class NewTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    category: str = Field(default="", max_length=60)
    priority: Priority = "normal"
    patient_id: UUID | None = None
    assignee_id: UUID | None = None
    due_at: AwareDatetime | None = None
    call_id: UUID | None = None
    conversation_id: UUID | None = None
    appointment_id: UUID | None = None


class UpdateTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=60)
    priority: Priority | None = None
    status: Status | None = None
    # Present and null: nobody is assigned any more.
    assignee_id: UUID | None = None
    due_at: AwareDatetime | None = None


class TicketText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=1500)


class TicketView(BaseModel):
    id: UUID
    number: int
    subject: str
    description: str
    category: str
    priority: str
    status: str
    source: str
    patient_id: UUID | None
    patient_name: str | None
    contact: str | None
    call_id: UUID | None
    conversation_id: UUID | None
    appointment_id: UUID | None
    assignee_id: UUID | None
    assignee_name: str | None
    due_at: datetime | None
    overdue: bool
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TicketEventView(BaseModel):
    id: UUID
    kind: str
    body: str
    author: str
    author_name: str | None
    # For a reply to the patient: queued, sent, failed.
    delivery: str | None
    created_at: datetime


class TicketDetail(TicketView):
    events: list[TicketEventView]
    # Whether a reply can reach the patient, and how.
    reply_by: Literal["sms", "email"] | None


class TicketCounts(BaseModel):
    open: int
    mine: int
    unassigned: int
    overdue: int


async def open_ticket(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    source: str,
    subject: str,
    description: str = "",
    category: str = "",
    priority: str = "normal",
    patient_id: UUID | None = None,
    contact: str | None = None,
    call_id: UUID | None = None,
    conversation_id: UUID | None = None,
    appointment_id: UUID | None = None,
    assignee_id: UUID | None = None,
    due_at: datetime | None = None,
    dedupe_key: str | None = None,
    author: str = "system",
    author_user_id: UUID | None = None,
) -> Ticket:
    """Add a ticket inside the caller's transaction, which already holds the tenant scope.

    With a `dedupe_key`, the same cause opens one ticket however often it is reported.
    """
    if dedupe_key is not None:
        existing = await session.scalar(
            select(Ticket).where(Ticket.tenant_id == tenant_id, Ticket.dedupe_key == dedupe_key)
        )
        if existing is not None:
            return existing
    # Numbers count up per clinic; the lock makes two tickets at once take turns.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"ticket-number:{tenant_id}"},
    )
    number = await session.scalar(
        select(func.coalesce(func.max(Ticket.number), 0) + 1).where(Ticket.tenant_id == tenant_id)
    )
    ticket = Ticket(
        id=uuid4(),
        tenant_id=tenant_id,
        number=int(number or 1),
        subject=subject.strip()[:200] or "Request",
        description=description.strip()[:4000],
        category=category[:60],
        priority=priority,
        source=source,
        patient_id=patient_id,
        contact=contact,
        call_id=call_id,
        conversation_id=conversation_id,
        appointment_id=appointment_id,
        assignee_id=assignee_id,
        due_at=due_at,
        dedupe_key=dedupe_key,
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(
            tenant_id=tenant_id,
            ticket_id=ticket.id,
            kind="created",
            author=author,
            author_user_id=author_user_id,
        )
    )
    return ticket


class TicketService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        view: Literal["open", "mine", "unassigned", "overdue", "resolved", "all"] = "open",
        search: str | None = None,
        patient_id: UUID | None = None,
    ) -> Sequence[TicketView]:
        require_permission(context, Permission.CALL_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            query = select(Ticket).where(Ticket.tenant_id == context.tenant_id)
            if view == "resolved":
                query = query.where(Ticket.status == "resolved")
            elif view != "all":
                query = query.where(Ticket.status != "resolved")
            if view == "mine":
                query = query.where(Ticket.assignee_id == context.actor_id)
            elif view == "unassigned":
                query = query.where(Ticket.assignee_id.is_(None))
            elif view == "overdue":
                query = query.where(Ticket.due_at < func.now())
            if patient_id is not None:
                query = query.where(Ticket.patient_id == patient_id)
            if search:
                needle = f"%{search.strip().lower()}%"
                query = query.where(
                    or_(
                        func.lower(Ticket.subject).like(needle),
                        cast(Ticket.number, String).like(needle),
                    )
                )
            rows = (
                await session.scalars(
                    query.order_by(Ticket.created_at.desc(), Ticket.id).limit(limit).offset(offset)
                )
            ).all()
            return await self._views(session, rows)

    async def counts(self, context: RequestContext) -> TicketCounts:
        require_permission(context, Permission.CALL_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            unresolved = Ticket.status != "resolved"
            row = (
                await session.execute(
                    select(
                        func.count().filter(unresolved),
                        func.count().filter(unresolved, Ticket.assignee_id == context.actor_id),
                        func.count().filter(unresolved, Ticket.assignee_id.is_(None)),
                        func.count().filter(unresolved, Ticket.due_at < func.now()),
                    ).where(Ticket.tenant_id == context.tenant_id)
                )
            ).one()
            return TicketCounts(open=row[0], mine=row[1], unassigned=row[2], overdue=row[3])

    async def get(self, context: RequestContext, identity: UUID) -> TicketDetail:
        require_permission(context, Permission.CALL_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            return await self._detail(session, await self._one(session, context, identity))

    async def create(self, context: RequestContext, command: NewTicket) -> TicketDetail:
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            ticket = await open_ticket(
                session,
                context.tenant_id,
                source="manual",
                author="staff",
                author_user_id=context.actor_id,
                **command.model_dump(),
            )
            return await self._detail(session, ticket)

    async def update(
        self, context: RequestContext, identity: UUID, command: UpdateTicket
    ) -> TicketDetail:
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        changes = command.model_dump(exclude_unset=True)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            ticket = await self._one(session, context, identity, lock=True)
            if "status" in changes and changes["status"] != ticket.status:
                ticket.status = changes["status"]
                ticket.resolved_at = datetime.now(UTC) if ticket.status == "resolved" else None
                self._event(session, context, ticket, "status", STATUS_LABELS[ticket.status])
            if "assignee_id" in changes and changes["assignee_id"] != ticket.assignee_id:
                name = "Nobody"
                if changes["assignee_id"] is not None:
                    name = await session.scalar(
                        select(User.display_name).where(
                            User.tenant_id == context.tenant_id,
                            User.id == changes["assignee_id"],
                            User.is_active.is_(True),
                        )
                    )
                    if name is None:
                        raise NotFoundError()
                ticket.assignee_id = changes["assignee_id"]
                self._event(session, context, ticket, "assignment", name)
            if "priority" in changes and changes["priority"] != ticket.priority:
                ticket.priority = changes["priority"]
                self._event(session, context, ticket, "priority", ticket.priority.capitalize())
            for field in ("subject", "category", "due_at"):
                if field in changes and (changes[field] is not None or field == "due_at"):
                    setattr(ticket, field, changes[field])
            await session.flush()
            return await self._detail(session, ticket)

    async def note(self, context: RequestContext, identity: UUID, command: TicketText) -> None:
        """Something for colleagues only; the patient never sees it."""
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            ticket = await self._one(session, context, identity, lock=True)
            self._event(session, context, ticket, "note", command.body)

    async def reply(self, context: RequestContext, identity: UUID, command: TicketText) -> None:
        """Send the patient a message about the ticket, by text or else by email."""
        require_permission(context, Permission.FOLLOW_UP_MANAGE)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            ticket = await self._one(session, context, identity, lock=True)
            channel, recipient = await self._reach(session, ticket)
            messages = MessageRepository(session, context)
            sender = await messages.sender_number() if channel == "sms" else None
            if channel is None or recipient is None or (channel == "sms" and sender is None):
                raise NoWayToReplyError()
            event_id = uuid4()
            outbound = await messages.enqueue(
                channel=channel,
                kind="ticket_reply",
                recipient=recipient,
                sender=sender,
                subject=f"Re: {ticket.subject}"[:200] if channel == "email" else None,
                body=command.body,
                dedupe_key=f"ticket:{event_id}",
            )
            session.add(
                TicketEvent(
                    id=event_id,
                    tenant_id=context.tenant_id,
                    ticket_id=ticket.id,
                    kind="reply",
                    body=command.body,
                    author="staff",
                    author_user_id=context.actor_id,
                    outbound_id=outbound,
                )
            )
            if ticket.status == "open":
                ticket.status = "waiting"

    @staticmethod
    def _event(
        session: AsyncSession, context: RequestContext, ticket: Ticket, kind: str, body: str
    ) -> None:
        session.add(
            TicketEvent(
                tenant_id=context.tenant_id,
                ticket_id=ticket.id,
                kind=kind,
                body=body,
                author="staff",
                author_user_id=context.actor_id,
            )
        )

    @staticmethod
    async def _one(
        session: AsyncSession, context: RequestContext, identity: UUID, *, lock: bool = False
    ) -> Ticket:
        query = select(Ticket).where(Ticket.tenant_id == context.tenant_id, Ticket.id == identity)
        ticket = await session.scalar(query.with_for_update() if lock else query)
        if ticket is None:
            raise NotFoundError()
        return ticket

    @staticmethod
    async def _reach(
        session: AsyncSession, ticket: Ticket
    ) -> tuple[Literal["sms", "email"] | None, str | None]:
        if ticket.patient_id is not None:
            patient = await session.scalar(
                select(Patient).where(
                    Patient.tenant_id == ticket.tenant_id, Patient.id == ticket.patient_id
                )
            )
            if patient is not None:
                if patient.phone and not patient.sms_opt_out:
                    return "sms", patient.phone
                if patient.email:
                    return "email", patient.email
        if ticket.contact:
            return "sms", ticket.contact
        return None, None

    async def _views(self, session: AsyncSession, rows: Sequence[Ticket]) -> Sequence[TicketView]:
        patients = {row.patient_id for row in rows if row.patient_id is not None}
        users = {row.assignee_id for row in rows if row.assignee_id is not None}
        patient_names: dict[UUID, str] = {}
        user_names: dict[UUID, str] = {}
        if patients:
            patient_names = {
                identity: name
                for identity, name in (
                    await session.execute(
                        select(Patient.id, Patient.display_name).where(Patient.id.in_(patients))
                    )
                ).all()
            }
        if users:
            user_names = {
                identity: name
                for identity, name in (
                    await session.execute(
                        select(User.id, User.display_name).where(User.id.in_(users))
                    )
                ).all()
            }
        now = datetime.now(UTC)
        return [
            TicketView(
                id=row.id,
                number=row.number,
                subject=row.subject,
                description=row.description,
                category=row.category,
                priority=row.priority,
                status=row.status,
                source=row.source,
                patient_id=row.patient_id,
                patient_name=patient_names.get(row.patient_id) if row.patient_id else None,
                contact=row.contact,
                call_id=row.call_id,
                conversation_id=row.conversation_id,
                appointment_id=row.appointment_id,
                assignee_id=row.assignee_id,
                assignee_name=user_names.get(row.assignee_id) if row.assignee_id else None,
                due_at=row.due_at,
                overdue=row.status != "resolved" and row.due_at is not None and row.due_at < now,
                resolved_at=row.resolved_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def _detail(self, session: AsyncSession, ticket: Ticket) -> TicketDetail:
        await session.flush()
        view = (await self._views(session, [ticket]))[0]
        rows = (
            await session.execute(
                select(TicketEvent, User.display_name, OutboundMessage.status)
                .outerjoin(User, User.id == TicketEvent.author_user_id)
                .outerjoin(OutboundMessage, OutboundMessage.id == TicketEvent.outbound_id)
                .where(
                    TicketEvent.tenant_id == ticket.tenant_id, TicketEvent.ticket_id == ticket.id
                )
                .order_by(TicketEvent.created_at, TicketEvent.id)
            )
        ).all()
        channel, _ = await self._reach(session, ticket)
        return TicketDetail(
            **view.model_dump(),
            events=[
                TicketEventView(
                    id=event.id,
                    kind=event.kind,
                    body=event.body,
                    author=event.author,
                    author_name=name,
                    delivery=delivery,
                    created_at=event.created_at,
                )
                for event, name, delivery in rows
            ],
            reply_by=channel,
        )
