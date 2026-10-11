"""The receptionist by text, the staff who take a thread over, tickets and the test chat."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.ai.llm.base import ChatRequest, LLMUnavailableError, ToolCall, ToolResult
from app.ai.llm.gateway import TurnLimits
from app.ai.models import AiTurn
from app.ai.runtime import AssistantRuntime
from app.appointments.models import Appointment
from app.calls.models import PhoneNumber
from app.clinic.schemas import ScheduleWindow, WeeklySchedule
from app.clinic.service import ClinicService
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import UnitOfWork
from app.locations.schemas import CreateLocation
from app.locations.service import LocationService
from app.messaging.service import ConversationService, InboundTexts, StaffText, TextAssistant
from app.messaging.test_chat import TestChat
from app.notifications.models import OutboundMessage
from app.patients.schemas import CreatePatient
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider
from app.providers.service import ProviderService
from app.tenants.models import Tenant
from app.tickets.service import NewTicket, TicketService, TicketText, UpdateTicket
from app.voice.circuit import CircuitRegistry
from tests.fake_llm import ScriptedModel, Step
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

PATIENT_NUMBER = "+919812345678"


class Clinic:
    """A live clinic with a number, one doctor who works every day, and one patient."""

    def __init__(self, tenancy: TenancyFixture) -> None:
        self.tenancy = tenancy
        self.tenant_id = tenancy.a.tenant_id
        self.owner = tenancy.context(tenancy.a)
        self.sessions = tenancy.database.session_factory
        self.number = "+1" + str(uuid4().int)[:10]
        self.provider_id: UUID = uuid4()
        self.patient_id: UUID = uuid4()

    async def open(self) -> "Clinic":
        uow = UnitOfWork(self.sessions)
        async with self.tenancy.admin.begin() as session:
            await set_tenant_scope(session, self.tenant_id)
            session.add(PhoneNumber(tenant_id=self.tenant_id, number=self.number))
            await session.execute(
                update(Tenant).where(Tenant.id == self.tenant_id).values(service_state="live")
            )
        location = await LocationService(uow).create(self.owner, CreateLocation(name="Main"))
        provider = await ProviderService(uow).create(
            self.owner, CreateProvider(location_id=location.id, display_name="Dr. Rao")
        )
        await ClinicService(uow).set_schedule(
            self.owner,
            provider.id,
            WeeklySchedule(
                windows=[
                    ScheduleWindow(weekday=day, starts="08:00", ends="20:00")  # pyright: ignore[reportArgumentType]
                    for day in range(7)
                ]
            ),
        )
        patient = await PatientService(uow).create(
            self.owner, CreatePatient(display_name="Meera Shah", phone=PATIENT_NUMBER)
        )
        self.provider_id, self.patient_id = provider.id, patient.id
        return self

    def runtime(self, *steps: Step) -> tuple[AssistantRuntime, ScriptedModel]:
        runtime = AssistantRuntime(
            self.sessions, self.tenancy.settings, None, CircuitRegistry(), TurnLimits()
        )
        model = ScriptedModel(*steps)
        runtime.substitute = model
        return runtime, model

    async def text(self, body: str) -> UUID:
        received = await InboundTexts(self.sessions).receive(
            self.tenant_id, self.number, PATIENT_NUMBER, body, "SM" + uuid4().hex
        )
        assert received.answer
        return received.conversation_id

    async def rows(self, model: Any) -> list[Any]:
        async with self.tenancy.admin.begin() as session:
            await set_tenant_scope(session, self.tenant_id)
            return list(
                await session.scalars(
                    select(model)
                    .where(model.tenant_id == self.tenant_id)
                    .order_by(model.created_at)
                )
            )

    def availability(self) -> Step:
        zone = ZoneInfo("Asia/Kolkata")
        day = (datetime.now(zone) + timedelta(days=3)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return [
            ToolCall(
                "c1",
                "check_availability",
                {
                    "provider_id": str(self.provider_id),
                    "starts_at": day.isoformat(),
                    "ends_at": (day + timedelta(days=1)).isoformat(),
                },
            )
        ]


def book_first_offered(request: ChatRequest) -> Step:
    offered = request.turns[-1]
    assert isinstance(offered, ToolResult) and offered.content["status"] == "available"
    slot = offered.content["items"][0]
    return [
        ToolCall(
            "c2",
            "book_appointment",
            {"provider_id": slot["provider_id"], "starts_at": slot["starts_at"]},
        )
    ]


async def test_a_patient_books_by_text_and_staff_take_the_thread_over(
    tenancy: TenancyFixture,
) -> None:
    clinic = await Clinic(tenancy).open()
    runtime, model = clinic.runtime(clinic.availability(), book_first_offered)
    assistant = TextAssistant(runtime)

    # A known number is that patient; the model only proposes, the server asks.
    thread = await clinic.text("Can I see Dr Rao in three days?")
    assert await assistant.answer(clinic.tenant_id, thread) is False
    asked = (await clinic.rows(OutboundMessage))[-1]
    assert (asked.kind, asked.recipient, asked.sender) == ("reply", PATIENT_NUMBER, clinic.number)
    assert "Dr. Rao" in asked.body and "Say yes to confirm" in asked.body
    # Times are worded for people, never as raw timestamps.
    assert "T0" not in asked.body and "+05:30" not in asked.body
    assert await clinic.rows(Appointment) == []

    # A plain yes is answered by the server itself: the model is not asked again.
    await clinic.text("YES")
    await assistant.answer(clinic.tenant_id, thread)
    assert len(model.requests) == 2
    booked = await clinic.rows(Appointment)
    assert [(row.patient_id, row.provider_id, row.status) for row in booked] == [
        (clinic.patient_id, clinic.provider_id, "booked")
    ]
    assert "is booked for" in (await clinic.rows(OutboundMessage))[-3].body or any(
        "is booked for" in row.body for row in await clinic.rows(OutboundMessage)
    )
    turns = await clinic.rows(AiTurn)
    assert [(row.channel, row.outcome) for row in turns] == [
        ("sms", "confirmation"),
        ("sms", "confirmation"),
    ]
    assert turns[0].tools == ["check_availability", "book_appointment"]

    # Staff see the thread, write in it (which takes it over) and hand it back.
    threads = ConversationService(clinic.sessions)
    listed = await threads.list(clinic.owner, 10, 0)
    assert [(row.patient_name, row.handler, row.unread) for row in listed] == [
        ("Meera Shah", "assistant", 2)
    ]
    detail = await threads.reply(clinic.owner, thread, StaffText(body="See you then, Meera."))
    assert [message.author for message in detail.messages] == [
        "patient",
        "assistant",
        "patient",
        "assistant",
        "staff",
    ]
    assert (detail.handler, detail.unread, detail.messages[-1].delivery) == ("staff", 0, "queued")
    # While staff hold the thread the receptionist stays out of it.
    received = await InboundTexts(clinic.sessions).receive(
        clinic.tenant_id, clinic.number, PATIENT_NUMBER, "Thanks!", "SM" + uuid4().hex
    )
    assert received.answer is False
    assert (await threads.hand_back(clinic.owner, thread)).handler == "assistant"
    # The other clinic sees none of it.
    assert await threads.list(tenancy.context(tenancy.b), 10, 0) == []


async def test_when_the_model_is_down_the_patient_is_told_and_a_ticket_waits(
    tenancy: TenancyFixture,
) -> None:
    clinic = await Clinic(tenancy).open()
    down = LLMUnavailableError("down")
    runtime, _ = clinic.runtime(down, down, down)
    thread = await clinic.text("Do you do blood tests?")
    await TextAssistant(runtime).answer(clinic.tenant_id, thread)

    sent = (await clinic.rows(OutboundMessage))[-1]
    assert "passed it to the team" in sent.body
    threads, tickets = ConversationService(clinic.sessions), TicketService(clinic.sessions)
    assert (await threads.get(clinic.owner, thread)).handler == "staff"
    waiting = await tickets.list(clinic.owner, 10, 0)
    assert [
        (row.source, row.conversation_id, row.patient_name, row.priority) for row in waiting
    ] == [("sms", thread, "Meera Shah", "high")]
    assert (await clinic.rows(AiTurn))[-1].outcome == "unavailable"

    # Staff work the ticket: a note for colleagues, a reply to the patient, then done.
    ticket = waiting[0]
    await tickets.note(clinic.owner, ticket.id, TicketText(body="Lab is open on weekdays."))
    await tickets.reply(clinic.owner, ticket.id, TicketText(body="Yes, weekdays 8 to 11."))
    reply = (await clinic.rows(OutboundMessage))[-1]
    assert (reply.kind, reply.channel, reply.recipient) == ("ticket_reply", "sms", PATIENT_NUMBER)
    detail = await tickets.update(
        clinic.owner, ticket.id, UpdateTicket(status="resolved", assignee_id=tenancy.a.user_id)
    )
    kinds = [event.kind for event in detail.events]
    assert kinds[:3] == ["created", "note", "reply"]
    assert sorted(kinds[3:]) == ["assignment", "status"]
    assert detail.reply_by == "sms" and detail.assignee_name == "Admin A"
    assert (await tickets.counts(clinic.owner)).open == 0
    # A ticket opened by hand gets the next number.
    manual = await tickets.create(clinic.owner, NewTicket(subject="Call the lab"))
    assert (manual.number, manual.source) == (ticket.number + 1, "manual")


async def test_the_test_chat_looks_things_up_but_changes_nothing(tenancy: TenancyFixture) -> None:
    clinic = await Clinic(tenancy).open()
    runtime, _ = clinic.runtime(clinic.availability(), book_first_offered)
    chat = TestChat(runtime)
    prepared = await chat.prepare(clinic.owner, None)
    events = [event async for event in chat.run(prepared, "Book me with Dr Rao in three days")]
    names = [name for name, _ in events]
    assert names[0] == "start" and names[-1] == "done"
    assert ("working", {"label": "Checked free times"}) in events
    said = " ".join(data["text"] for name, data in events if name == "say")
    assert "test chat" in said
    assert await clinic.rows(Appointment) == [] and await clinic.rows(OutboundMessage) == []
    # The same chat goes on where it stopped.
    again = await chat.prepare(clinic.owner, prepared.session.conversation_id)
    assert again.session.conversation_id == prepared.session.conversation_id
    assert len(again.state.transcript_turns) == 2
