"""Tickets, text conversations, the test chat and what the receptionist cost."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import Date, cast, func, select

from app.ai.models import AiTurn
from app.ai.runtime import AssistantRuntime
from app.api.dependencies import get_resources
from app.api.tenancy import Context, Cursor, DatabaseDependency, Limit, Search
from app.auth.policies import Permission, require_permission
from app.calls.models import Call
from app.core.database.pages import Page, decode_cursor, page
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import NotFoundError
from app.core.security.rate_limit import enforce_rate_limit
from app.messaging.service import (
    ConversationDetail,
    ConversationService,
    ConversationView,
    StaffText,
)
from app.messaging.test_chat import TOOL_LABELS, ChatMessage, TestChat
from app.tickets.service import (
    NewTicket,
    TicketCounts,
    TicketDetail,
    TicketService,
    TicketText,
    TicketView,
    UpdateTicket,
)

router = APIRouter(prefix="/api/v1", tags=["assistant"])

STREAM_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
LIVE_POLL_SECONDS = 1.0
# Each test message is a real, billed request to the model.
TEST_MESSAGES_PER_MINUTE = 12
LIVE_MAX_SECONDS = 30 * 60


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def runtime(request: Request) -> AssistantRuntime:
    found = getattr(request.app.state, "assistant", None)
    if not isinstance(found, AssistantRuntime):
        raise HTTPException(503, "The assistant is not available")
    return found


# --- Tickets ----------------------------------------------------------------------


@router.get("/tickets")
async def list_tickets(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    view: Literal["open", "mine", "unassigned", "overdue", "resolved", "all"] = "open",
    q: Search = None,
    patient_id: UUID | None = None,
) -> Page[TicketView]:
    offset = decode_cursor(cursor)
    rows = await TicketService(database.session_factory).list(
        context, limit + 1, offset, view=view, search=q, patient_id=patient_id
    )
    return page(rows, limit, offset)


@router.get("/tickets/counts")
async def ticket_counts(context: Context, database: DatabaseDependency) -> TicketCounts:
    return await TicketService(database.session_factory).counts(context)


@router.post("/tickets", status_code=201)
async def create_ticket(
    command: NewTicket, context: Context, database: DatabaseDependency
) -> TicketDetail:
    return await TicketService(database.session_factory).create(context, command)


@router.get("/tickets/{ticket_id}")
async def get_ticket(
    ticket_id: UUID, context: Context, database: DatabaseDependency
) -> TicketDetail:
    return await TicketService(database.session_factory).get(context, ticket_id)


@router.patch("/tickets/{ticket_id}")
async def update_ticket(
    ticket_id: UUID, command: UpdateTicket, context: Context, database: DatabaseDependency
) -> TicketDetail:
    return await TicketService(database.session_factory).update(context, ticket_id, command)


@router.post("/tickets/{ticket_id}/notes", status_code=204)
async def add_ticket_note(
    ticket_id: UUID, command: TicketText, context: Context, database: DatabaseDependency
) -> Response:
    await TicketService(database.session_factory).note(context, ticket_id, command)
    return Response(status_code=204)


@router.post("/tickets/{ticket_id}/replies", status_code=204)
async def reply_on_ticket(
    ticket_id: UUID, command: TicketText, context: Context, database: DatabaseDependency
) -> Response:
    """Send the patient a message about the ticket, by text or else by email."""
    await TicketService(database.session_factory).reply(context, ticket_id, command)
    return Response(status_code=204)


# --- Text conversations -----------------------------------------------------------


class UnreadCount(BaseModel):
    count: int


@router.get("/conversations")
async def list_conversations(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    view: Literal["all", "unread", "mine", "staff"] = "all",
    q: Search = None,
) -> Page[ConversationView]:
    offset = decode_cursor(cursor)
    rows = await ConversationService(database.session_factory).list(
        context, limit + 1, offset, view=view, search=q
    )
    return page(rows, limit, offset)


@router.get("/conversations/unread")
async def unread_conversations(context: Context, database: DatabaseDependency) -> UnreadCount:
    return UnreadCount(count=await ConversationService(database.session_factory).unread(context))


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: UUID, context: Context, database: DatabaseDependency
) -> ConversationDetail:
    return await ConversationService(database.session_factory).get(context, conversation_id)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def reply_in_conversation(
    conversation_id: UUID, command: StaffText, context: Context, database: DatabaseDependency
) -> ConversationDetail:
    """Write to the patient as staff. This also takes the thread over from the assistant."""
    return await ConversationService(database.session_factory).reply(
        context, conversation_id, command
    )


@router.post("/conversations/{conversation_id}/take-over")
async def take_over_conversation(
    conversation_id: UUID, context: Context, database: DatabaseDependency
) -> ConversationDetail:
    return await ConversationService(database.session_factory).take_over(context, conversation_id)


@router.post("/conversations/{conversation_id}/hand-back")
async def hand_back_conversation(
    conversation_id: UUID, context: Context, database: DatabaseDependency
) -> ConversationDetail:
    return await ConversationService(database.session_factory).hand_back(context, conversation_id)


# --- Try your receptionist --------------------------------------------------------


@router.post("/assistant/test")
async def test_chat(command: ChatMessage, request: Request, context: Context) -> StreamingResponse:
    """One message to the receptionist, answered as a stream of events:

    `start`, then `say` for each sentence, `working` and `worked` around each lookup,
    `handoff` when it would pass the conversation to a person, and `done`.
    """
    await enforce_rate_limit(
        get_resources(request).redis,
        enabled=request.app.state.rate_limits,
        scope="assistant_test",
        identity=f"{context.tenant_id}:{context.support_session_id or context.actor_id}",
        limit=TEST_MESSAGES_PER_MINUTE,
    )
    chat = TestChat(runtime(request))
    prepared = await chat.prepare(context, command.conversation_id)

    async def events() -> AsyncIterator[str]:
        async for name, data in chat.run(prepared, command.message):
            yield sse(name, data)

    return StreamingResponse(events(), media_type="text/event-stream", headers=STREAM_HEADERS)


# --- What the receptionist did and cost -------------------------------------------


class TurnView(BaseModel):
    created_at: datetime
    outcome: str
    # What the receptionist looked up or did, in the words the screens use.
    steps: list[str]
    first_text_ms: int | None
    # On a call: until the caller heard the first sound of the reply.
    first_audio_ms: int | None = None
    interrupted: bool = False
    total_ms: int
    error: str | None


class DailyUsage(BaseModel):
    day: date
    replies: int
    input_tokens: int
    output_tokens: int
    # Milliseconds until the first words, averaged over replies that said something.
    first_text_ms: int | None
    handed_over: int
    failed: int


@router.get("/calls/{call_id}/turns")
async def call_turns(
    call_id: UUID, context: Context, database: DatabaseDependency
) -> list[TurnView]:
    """The receptionist's side of one call, reply by reply. No words, only what it did."""
    require_permission(context, Permission.CALL_READ)
    async with database.session_factory.begin() as session:
        await enter_tenant_scope(session, context)
        rows = (
            await session.scalars(
                select(AiTurn)
                .where(AiTurn.tenant_id == context.tenant_id, AiTurn.call_id == call_id)
                .order_by(AiTurn.created_at)
                .limit(200)
            )
        ).all()
        return [
            TurnView(
                created_at=row.created_at,
                outcome=row.outcome,
                steps=[TOOL_LABELS.get(str(tool), str(tool)) for tool in row.tools],
                first_text_ms=row.first_text_ms,
                first_audio_ms=row.first_audio_ms,
                interrupted=row.interrupted,
                total_ms=row.total_ms,
                error=row.error,
            )
            for row in rows
        ]


@router.get("/analytics/assistant")
async def assistant_usage(
    context: Context,
    database: DatabaseDependency,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date, Query(alias="to")],
) -> list[DailyUsage]:
    require_permission(context, Permission.CALL_READ)
    if end < start or (end - start).days > 366:
        raise HTTPException(422, "Invalid analytics date range")
    day = cast(AiTurn.created_at, Date)
    async with database.session_factory.begin() as session:
        await enter_tenant_scope(session, context)
        rows = await session.execute(
            select(
                day,
                func.count(),
                func.coalesce(func.sum(AiTurn.input_tokens), 0),
                func.coalesce(func.sum(AiTurn.output_tokens), 0),
                func.avg(AiTurn.first_text_ms),
                func.count().filter(AiTurn.outcome == "handoff"),
                func.count().filter(AiTurn.outcome.in_(("unavailable", "blocked", "interrupted"))),
            )
            .where(
                AiTurn.tenant_id == context.tenant_id,
                AiTurn.channel != "test",
                AiTurn.created_at >= datetime.combine(start, time.min, UTC),
                AiTurn.created_at < datetime.combine(end + timedelta(days=1), time.min, UTC),
            )
            .group_by(day)
            .order_by(day)
        )
        return [
            DailyUsage(
                day=row[0],
                replies=row[1],
                input_tokens=int(row[2]),
                output_tokens=int(row[3]),
                first_text_ms=int(row[4]) if row[4] is not None else None,
                handed_over=row[5],
                failed=row[6],
            )
            for row in rows
        ]


# --- A call as it happens ---------------------------------------------------------


@router.get("/calls/{call_id}/live")
async def live_transcript(
    call_id: UUID, request: Request, context: Context, database: DatabaseDependency
) -> StreamingResponse:
    """What is being said on a call in progress, as `turn` events, then `ended`."""
    require_permission(context, Permission.CALL_READ)
    async with database.session_factory.begin() as session:
        await enter_tenant_scope(session, context)
        known = await session.scalar(
            select(Call.id).where(Call.tenant_id == context.tenant_id, Call.id == call_id)
        )
    if known is None:
        raise NotFoundError()
    store = request.app.state.conversation_store
    tenant_id = context.tenant_id

    async def events() -> AsyncIterator[str]:
        sent = 0
        deadline = asyncio.get_running_loop().time() + LIVE_MAX_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if await request.is_disconnected():
                return
            state = await store.get(tenant_id, call_id) if store is not None else None
            if state is None:
                yield sse("ended", {})
                return
            # The transcript keeps a bounded window; resend from the start if it moved.
            turns = state.transcript_turns
            sent = min(sent, len(turns))
            for turn in turns[sent:]:
                yield sse("turn", {"speaker": turn.speaker, "text": turn.text})
            sent = len(turns)
            if state.call_state.ended_at is not None:
                yield sse("ended", {})
                return
            await asyncio.sleep(LIVE_POLL_SECONDS)

    return StreamingResponse(events(), media_type="text/event-stream", headers=STREAM_HEADERS)
