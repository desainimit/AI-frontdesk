"""A trusted caller's earlier calls and coming appointments, read under the call's own
database identity."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.ai.memory import CallerHistory
from app.ai.session import Session
from app.calls.service import CallService
from app.conversation.manager import ConversationStateManager
from app.conversation.state import IdentifierType
from app.core.database.rls import set_tenant_scope
from app.postcall.models import PostCallReport
from app.settings.service import SettingsService
from tests.evals.harness import EvalClinic
from tests.integration.clinic import PATIENT_NUMBER
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_history_of_a_trusted_caller(tenancy: TenancyFixture) -> None:
    clinic = await EvalClinic(tenancy).open()
    await clinic.book_for_patient()
    calls = CallService(clinic.sessions)
    earlier = await calls.receive(clinic.number, "CA" + uuid4().hex, uuid4(), caller=PATIENT_NUMBER)
    await calls.finish(earlier, "completed", "stream_stopped")
    async with tenancy.admin.begin() as database:
        await set_tenant_scope(database, clinic.tenant_id)
        database.add(
            PostCallReport(
                tenant_id=clinic.tenant_id,
                call_id=earlier.call_id,
                call_ended_at=datetime.now(UTC) - timedelta(days=2),
                summary="Asked to move her visit with Dr. Rao; moved to later the same day.",
                outcome={},
                transcript_digest="0" * 64,
                turn_count=4,
            )
        )
    now = await calls.receive(clinic.number, "CA" + uuid4().hex, uuid4(), caller=PATIENT_NUMBER)
    configuration = (await SettingsService(clinic.sessions).get(clinic.owner)).settings
    session = Session(
        clinic.tenant_id,
        now.call_id,
        "voice",
        now.request_id,
        configuration,
        configuration.receptionist.allowed_tools,
        provider_call_id=now.provider_call_id,
        contact=PATIENT_NUMBER,
    )
    state = ConversationStateManager.create_initial_state(
        now.call_id, clinic.tenant_id, now.provider_call_id, caller_phone=PATIENT_NUMBER
    )
    state.identity_state.record_attempt(True, clinic.patient_id)
    state.identity_state.identifier_type = IdentifierType.ACCESS_CODE

    history = await CallerHistory(clinic.sessions).load(session, state)

    lines = history.splitlines()
    assert lines[0].startswith("Call on ") and "moved to later the same day" in lines[0]
    assert lines[1].startswith("Coming appointment: ") and lines[1].endswith("with Dr. Rao")
    # The clinic can turn it off.
    quiet = configuration.model_copy(
        update={"privacy": configuration.privacy.model_copy(update={"remember_callers": False})}
    )
    assert (
        await CallerHistory(clinic.sessions).load(
            Session(
                clinic.tenant_id,
                now.call_id,
                "voice",
                now.request_id,
                quiet,
                quiet.receptionist.allowed_tools,
                provider_call_id=now.provider_call_id,
                contact=PATIENT_NUMBER,
            ),
            state,
        )
        == ""
    )
