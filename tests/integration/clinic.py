"""A live clinic for tests of the receptionist: a number, one doctor, one patient."""

from datetime import datetime, timedelta
from typing import Any, Self
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.ai.llm.base import ToolCall
from app.ai.llm.gateway import TurnLimits
from app.ai.runtime import AssistantRuntime
from app.calls.models import PhoneNumber
from app.clinic.schemas import ScheduleWindow, WeeklySchedule
from app.clinic.service import ClinicService
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import UnitOfWork
from app.locations.schemas import CreateLocation
from app.locations.service import LocationService
from app.messaging.service import InboundTexts
from app.patients.schemas import CreatePatient
from app.patients.service import PatientService
from app.providers.schemas import CreateProvider
from app.providers.service import ProviderService
from app.tenants.models import Tenant
from app.voice.circuit import CircuitRegistry
from tests.fake_llm import ScriptedModel, Step
from tests.integration.test_tenancy import TenancyFixture

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

    async def open(self) -> Self:
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
