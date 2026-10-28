"""The receptionist by text, the staff who take a thread over, tickets and the test chat."""

from uuid import uuid4

import pytest

from app.ai.llm.base import ChatRequest, LLMUnavailableError, ToolCall, ToolResult
from app.ai.models import AiTurn
from app.appointments.models import Appointment
from app.messaging.service import ConversationService, InboundTexts, StaffText, TextAssistant
from app.messaging.test_chat import TestChat
from app.notifications.models import OutboundMessage
from app.settings.schemas import SettingsSection
from app.settings.service import SettingsService
from app.tickets.service import NewTicket, TicketService, TicketText, UpdateTicket
from tests.fake_llm import Step
from tests.integration.clinic import PATIENT_NUMBER, Clinic
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


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


@pytest.mark.parametrize("answer,books", [("હા", True), ("ના", False), ("હા પણ કાલે", False)])
async def test_gujarati_booking_requires_current_explicit_confirmation(
    tenancy: TenancyFixture, answer: str, books: bool
) -> None:
    clinic = await Clinic(tenancy).open()
    settings = SettingsService(clinic.sessions, tenancy.settings)
    before = (await settings.get(clinic.owner)).settings
    await settings.save(
        clinic.owner,
        SettingsSection.CALLS,
        {
            **before.calls.model_dump(mode="json"),
            "enabled": False,
        },
    )
    await settings.save(
        clinic.owner,
        SettingsSection.RECEPTIONIST,
        {
            **before.receptionist.model_dump(mode="json"),
            "languages": {
                "default_language": "en",
                "enabled_languages": ["en", "gu"],
                "auto_detect": True,
            },
        },
    )
    runtime, model = clinic.runtime(
        clinic.availability(), book_first_offered, "કૃપા કરીને નવો સમય જણાવો."
    )
    assistant = TextAssistant(runtime)
    thread = await clinic.text("મારે ડૉક્ટરની મુલાકાત બુક કરાવવી છે")
    await assistant.answer(clinic.tenant_id, thread)
    proposal = (await clinic.rows(OutboundMessage))[-1].body
    assert "Dr. Rao" in proposal and "બુક કરી દઉં?" in proposal
    assert "Dr. Rao" in proposal and "AM" not in proposal and "PM" not in proposal
    assert await clinic.rows(Appointment) == []
    await clinic.text(answer)
    await assistant.answer(clinic.tenant_id, thread)
    appointments = await clinic.rows(Appointment)
    assert len(appointments) == int(books)
    if books:
        assert appointments[0].patient_id == clinic.patient_id
        replies = [row.body for row in await clinic.rows(OutboundMessage) if row.kind == "reply"]
        assert any("બુક થઈ ગઈ છે" in body for body in replies)
        assert len(model.requests) == 2
    else:
        assert not any("બુક થઈ ગઈ છે" in row.body for row in await clinic.rows(OutboundMessage))
    other = ConversationService(clinic.sessions)
    assert await other.list(tenancy.context(tenancy.b), 10, 0) == []


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
    assert "Dr. Rao" in asked.body and "Shall I book it" in asked.body
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
    # The confirmation text goes out as the clinic's template words it.
    assert any("you're booked" in row.body for row in await clinic.rows(OutboundMessage))
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
