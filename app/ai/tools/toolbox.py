"""Everything the receptionist can look up or ask for, assembled for one process."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.identity import ContactIdentity
from app.ai.session import Session
from app.ai.tools.appointments import (
    AppointmentFlow,
    AppointmentsInput,
    AvailabilityInput,
    BookInput,
    CancelInput,
    NoInput,
    Outcome,
    RegisterInput,
    RescheduleInput,
    ToolInput,
)
from app.ai.tools.registry import Tool, ToolRegistry
from app.ai.wording import spoken
from app.calendar_sync.service import CalendarService
from app.calls.access import CallPatientAccess, IdentifyPatient
from app.clinic.models import Service
from app.conversation.state import ConversationState, SafetyDecision
from app.core.database.unit_of_work import UnitOfWork, enter_tenant_scope
from app.core.settings import Settings
from app.integrations.resolver import TenantIntegrationResolver
from app.knowledge.embeddings import open_embedder
from app.knowledge.schemas import KnowledgeQuery
from app.knowledge.service import KnowledgeService
from app.knowledge.storage import build_storage
from app.locations.models import Location
from app.providers.models import Provider
from app.safety import handover
from app.safety.handover_messages import handover_message
from app.tickets.service import open_ticket


class TicketInput(ToolInput):
    subject: str = Field(min_length=3, max_length=120, description="A few words: what is needed.")
    details: str = Field(default="", max_length=800, description="What staff need to know.")
    urgent: bool = False


class TransferInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: str = Field(default="", max_length=200)
    # One of the names the server listed as people the caller can be put through to.
    target: str = Field(default="", max_length=60)


class Listing(BaseModel):
    items: list[dict[str, Any]]


def build_tools(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    resolver: TenantIntegrationResolver | None = None,
    *,
    timeout_seconds: float = 5,
) -> tuple[ToolRegistry, AppointmentFlow, ContactIdentity]:
    identity = ContactIdentity(sessions)
    flow = AppointmentFlow(
        UnitOfWork(sessions),
        CallPatientAccess(sessions),
        identity,
        # A booking asks the provider's own calendar once more first.
        CalendarService(sessions, settings),
    )

    async def locations(session: Session, state: ConversationState, _: BaseModel) -> BaseModel:
        async with sessions.begin() as database:
            await enter_tenant_scope(database, session.request(state))
            rows = await database.scalars(
                select(Location)
                .where(Location.tenant_id == session.tenant_id)
                .order_by(Location.name)
                .limit(20)
            )
            return Listing(
                items=[
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "timezone": row.timezone,
                        "address": row.address or None,
                        "phone": row.phone or None,
                    }
                    for row in rows
                ]
            )

    async def providers(session: Session, state: ConversationState, _: BaseModel) -> BaseModel:
        async with sessions.begin() as database:
            await enter_tenant_scope(database, session.request(state))
            rows = await database.scalars(
                select(Provider)
                .where(Provider.tenant_id == session.tenant_id, Provider.is_active.is_(True))
                .order_by(Provider.display_name)
                .limit(20)
            )
            return Listing(
                items=[
                    {
                        "id": str(row.id),
                        "name": row.display_name,
                        "specialty": row.specialty,
                        "location_id": str(row.location_id),
                    }
                    for row in rows
                ]
            )

    async def services(session: Session, state: ConversationState, _: BaseModel) -> BaseModel:
        async with sessions.begin() as database:
            await enter_tenant_scope(database, session.request(state))
            rows = await database.scalars(
                select(Service)
                .where(Service.tenant_id == session.tenant_id, Service.is_active.is_(True))
                .order_by(Service.name)
                .limit(20)
            )
            return Listing(
                items=[
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "description": row.description[:300] or None,
                        "minutes": row.duration_minutes,
                    }
                    for row in rows
                ]
            )

    async def knowledge(
        session: Session, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        query = KnowledgeQuery.model_validate(payload.model_dump())
        embedder = await open_embedder(settings, resolver, session.tenant_id)
        try:
            service = KnowledgeService(UnitOfWork(sessions), build_storage(settings), embedder)
            return await service.search(session.request(state), query.question, query.limit)
        finally:
            await embedder.close()

    async def ticket(session: Session, state: ConversationState, payload: BaseModel) -> BaseModel:
        command = TicketInput.model_validate(payload.model_dump())
        if not session.writes:
            return Outcome(status="test_mode", say=spoken(state.language, "test_mode"))
        async with sessions.begin() as database:
            await enter_tenant_scope(database, session.request(state))
            row = await open_ticket(
                database,
                session.tenant_id,
                source="call" if session.channel == "voice" else "sms",
                subject=command.subject,
                description=command.details,
                priority="high" if command.urgent else "normal",
                patient_id=state.identity_state.patient_id
                if state.identity_state.is_verified
                else None,
                contact=session.contact,
                call_id=session.conversation_id if session.channel == "voice" else None,
                conversation_id=None if session.channel == "voice" else session.conversation_id,
                author="assistant",
            )
            return Outcome(status="message_taken", items=[{"ticket_number": row.number}])

    async def transfer(session: Session, state: ConversationState, payload: BaseModel) -> BaseModel:
        safety = state.safety_state
        wanted = payload if isinstance(payload, TransferInput) else TransferInput()
        if session.channel == "voice":
            # On a call the caller is asked first. Called again in a later turn, after
            # the offer, it means they agreed.
            targets = handover.targets_now(session.configuration, state)
            target = handover.choose(targets, wanted.target)
            if handover.pending(state) and state.utterance_count > safety.offered_turn:
                if target is not None:
                    safety.transfer_target = target.name
                handover.agree(state, can_transfer=target is not None)
                safety.source = "assistant"
                return Outcome(status="handing_over")
            handover.offer(state, target, wanted.reason or state.last_caller_utterance)
            if target is None:
                return Outcome(
                    status="offered", say=handover_message(state.language, "offer_message")
                )
            return Outcome(
                status="offered",
                say=handover_message(state.language, "offer", target=target.name),
            )
        # By text, the team is told and gets back to the person.
        safety.decision = SafetyDecision.ESCALATE
        safety.reason, safety.source = "human_requested", "assistant"
        safety.triggered_at = datetime.now(UTC)
        return Outcome(status="handing_over")

    async def end_call(session: Session, state: ConversationState, _: BaseModel) -> BaseModel:
        if session.channel != "voice":
            return Outcome(status="not_available")
        if state.awaiting_confirmation or handover.pending(state):
            # A question to the caller is still open; it is answered first.
            return Outcome(status="not_now", next="answer_the_open_question_first")
        state.end_requested = True
        return Outcome(status="ending", next="say_a_short_warm_goodbye")

    def propose(action: Any) -> Any:
        async def handler(
            session: Session, state: ConversationState, payload: BaseModel
        ) -> BaseModel:
            return await flow.propose(session, state, action, payload)

        return handler

    tools = [
        Tool(
            "list_services",
            "The appointment types this clinic offers, with their length in minutes. Pass an "
            "id as service_id when checking availability or booking.",
            NoInput,
            services,
        ),
        Tool(
            "list_locations", "The clinic's locations, addresses and timezones.", NoInput, locations
        ),
        Tool(
            "list_providers",
            "The clinic's doctors, their specialty and location.",
            NoInput,
            providers,
        ),
        Tool(
            "get_knowledge",
            "Find facts the clinic has published (services, prices, policies, directions). "
            "Never for live availability, appointments or patient details.",
            KnowledgeQuery,
            knowledge,
        ),
        Tool(
            "identify_patient",
            "Redeem an 8-digit code reception gave the caller. Never guess a code.",
            IdentifyPatient,
            flow.identify,
        ),
        Tool(
            "register_patient",
            "Add the person as a new patient under the number they are using. Only after a "
            "tool answered identity_required and they told you their full name.",
            RegisterInput,
            flow.register,
        ),
        Tool(
            "check_availability",
            "Free times for one doctor in a range of at most 31 days (timestamps with a UTC "
            "offset). Returns up to 20.",
            AvailabilityInput,
            flow.slots,
        ),
        Tool(
            "list_my_appointments",
            "The person's own appointments: upcoming ones (to tell them, or to pick one to "
            "move or cancel) or past visits (when they ask about an earlier visit).",
            AppointmentsInput,
            flow.list_appointments,
        ),
        Tool(
            "book_appointment",
            "Propose a booking at a time check_availability returned. The server asks the "
            "person to confirm.",
            BookInput,
            propose("book_appointment"),
        ),
        Tool(
            "cancel_appointment",
            "Propose cancelling one of the person's appointments. The server asks to confirm.",
            CancelInput,
            propose("cancel_appointment"),
        ),
        Tool(
            "reschedule_appointment",
            "Propose a new time for one of the person's appointments. The server asks to confirm.",
            RescheduleInput,
            propose("reschedule_appointment"),
        ),
        Tool(
            "create_ticket",
            "Leave a message for the clinic's team, who will get back to the person. Tell the "
            "person you have done so.",
            TicketInput,
            ticket,
        ),
        Tool(
            "transfer_to_staff",
            "Offer to put the person through to the clinic team, or to one of the people "
            "listed as reachable, by name. Use it when they ask for a person, or when you "
            "have tried and cannot help with what they need. The server asks them first; "
            "call it again only once they have agreed.",
            TransferInput,
            transfer,
        ),
        Tool(
            "end_call",
            "End the call once the person has said goodbye and has nothing more to ask. "
            "Then say a short, warm goodbye. Never while a question to them is still open.",
            NoInput,
            end_call,
        ),
    ]
    return ToolRegistry(tools, timeout_seconds), flow, identity
