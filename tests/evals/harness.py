"""Run a scenario against a fresh clinic and say whether it was handled.

The patient writes by text message, because that path exercises everything: the engine,
the tools, the confirmation, what is stored, and what staff are left with.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.ai.llm.base import LLMProvider
from app.ai.models import AiTurn
from app.appointments.commands import BookAppointment
from app.appointments.models import Appointment
from app.appointments.service import AppointmentService
from app.core.database.unit_of_work import UnitOfWork
from app.messaging.models import Conversation, ConversationMessage
from app.messaging.service import InboundTexts, TextAssistant
from app.patients.schemas import CreatePatient
from app.patients.service import PatientService
from app.settings.schemas import SettingsSection
from app.settings.service import SettingsService
from app.tickets.models import Ticket
from tests.evals.scenarios import Scenario
from tests.fake_llm import Step
from tests.integration.clinic import PATIENT_NUMBER, Clinic
from tests.integration.test_tenancy import TenancyFixture

STRANGER_NUMBER = "+919700000001"


class EvalClinic(Clinic):
    async def book_for_patient(self, *, other_patient: bool = False) -> None:
        """The patient already has an appointment, in three days at half past ten."""
        uow = UnitOfWork(self.sessions)
        if other_patient:
            await PatientService(uow).create(
                self.owner, CreatePatient(display_name="Zed Other", phone="+919822223333")
            )
        start = (datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=3)).replace(
            hour=10, minute=30, second=0, microsecond=0
        )
        await AppointmentService(uow).book(
            self.owner,
            BookAppointment(
                provider_id=self.provider_id, patient_id=self.patient_id, starts_at=start
            ),
            f"eval-{uuid4().hex}",
        )

    async def offer_languages(self, *codes: str) -> None:
        settings = SettingsService(self.sessions)
        section = (await settings.get(self.owner)).settings.receptionist.model_dump(mode="json")
        section["languages"] = {
            "default_language": codes[0],
            "enabled_languages": list(codes),
            "auto_detect": True,
        }
        await settings.save(self.owner, SettingsSection.RECEPTIONIST, section)


@dataclass
class Result:
    scenario: str
    safety: bool
    problems: list[str] = field(default_factory=lambda: list[str]())
    first_text_ms: list[int] = field(default_factory=lambda: list[int]())
    replies: list[str] = field(default_factory=lambda: list[str]())
    # US dollars for the model: replies and the quick checks beside them.
    cost_usd: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.problems


async def run(
    scenario: Scenario, tenancy: TenancyFixture, live: LLMProvider | None = None
) -> Result:
    clinic = await EvalClinic(tenancy).open()
    if scenario.before is not None:
        await scenario.before(clinic)
    steps: tuple[Step, ...] = () if live is not None else scenario.script(clinic)
    runtime, _ = clinic.runtime(*steps)
    if live is not None:
        runtime.substitute = live
    assistant, inbound = TextAssistant(runtime), InboundTexts(clinic.sessions)
    sender = STRANGER_NUMBER if scenario.stranger else PATIENT_NUMBER
    for said in scenario.says:
        received = await inbound.receive(
            clinic.tenant_id, clinic.number, sender, said, "SM" + uuid4().hex
        )
        if received.answer:
            await assistant.answer(clinic.tenant_id, received.conversation_id)

    result, expect = Result(scenario.name, scenario.safety), scenario.expect
    turns: list[AiTurn] = await clinic.rows(AiTurn)
    messages: list[ConversationMessage] = await clinic.rows(ConversationMessage)
    appointments: list[Appointment] = await clinic.rows(Appointment)
    result.replies = [row.body for row in messages if row.author == "assistant"]
    result.first_text_ms = [row.first_text_ms for row in turns if row.first_text_ms is not None]
    result.cost_usd = sum(float(row.cost_usd) + float(row.checks_cost_usd) for row in turns)
    used = {str(tool) for row in turns for tool in row.tools}
    last = result.replies[-1] if result.replies else ""

    def expected(label: str, wanted: object, got: object) -> None:
        if wanted is not None and wanted != got:
            result.problems.append(f"{label}: expected {wanted}, got {got}")

    if not result.replies:
        result.problems.append("the patient got no reply")
    if expect.outcome and (not turns or turns[-1].outcome not in expect.outcome):
        ended = turns[-1].outcome if turns else "nothing"
        result.problems.append(f"ended as {ended}, expected {' or '.join(expect.outcome)}")
    for tool in expect.used:
        if tool not in used:
            result.problems.append(f"never used {tool}")
    for tool in expect.not_used:
        if tool in used:
            result.problems.append(f"used {tool}")
    for pattern in expect.says:
        if not re.search(pattern, last, re.IGNORECASE):
            result.problems.append(f"the last reply does not match {pattern!r}")
    for pattern in expect.never_says:
        if any(re.search(pattern, reply, re.IGNORECASE) for reply in result.replies):
            result.problems.append(f"a reply matches {pattern!r}")
    expected("booked", expect.booked, sum(row.status == "booked" for row in appointments))
    expected("cancelled", expect.cancelled, sum(row.status == "cancelled" for row in appointments))
    if expect.tickets is not None:
        expected("tickets", expect.tickets, len(await clinic.rows(Ticket)))
    if expect.handler is not None:
        threads: list[Conversation] = await clinic.rows(Conversation)
        expected("handler", expect.handler, threads[-1].handler if threads else None)
    return result
