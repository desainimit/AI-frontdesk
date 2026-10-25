"""A trusted caller's earlier calls and coming appointments, read under the call's own
database identity."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update

from app.ai.memory import CallerHistory
from app.ai.session import Session
from app.calls.models import Call
from app.calls.service import CallService
from app.conversation.manager import ConversationStateManager
from app.conversation.state import IdentifierType
from app.core.database.rls import set_tenant_scope
from app.core.errors import NotFoundError
from app.postcall.models import PostCallReport
from app.postcall.schemas import PostCallInput
from app.postcall.service import PostCallQueryService
from app.settings.service import SettingsService
from tests.evals.harness import EvalClinic
from tests.integration.clinic import PATIENT_NUMBER
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_a_finished_call_reads_as_its_conversation(tenancy: TenancyFixture) -> None:
    clinic = await EvalClinic(tenancy).open()
    calls = CallService(clinic.sessions)
    context = await calls.receive(clinic.number, "CA" + uuid4().hex, uuid4(), caller=PATIENT_NUMBER)
    queries = PostCallQueryService(clinic.sessions)
    # Nothing saved yet: an empty thread, not an error.
    assert await queries.transcript(clinic.owner, context.call_id) == []
    state = ConversationStateManager.create_initial_state(
        context.call_id, clinic.tenant_id, context.provider_call_id
    )
    state.append_transcript_turn("agent", "Thanks for calling. How can I help?")
    state.append_transcript_turn("caller", "Can I move my visit? Mail me at meera@example.com")
    await calls.finish(
        context, "completed", "stream_stopped", post_call_input=PostCallInput.capture(state)
    )
    lines = await queries.transcript(clinic.owner, context.call_id)
    assert [line.speaker for line in lines] == ["agent", "caller"]
    # What is shown is what was saved: identifiers stay masked.
    assert lines[1].text == "Can I move my visit? Mail me at [email]"
    with pytest.raises(NotFoundError):
        await queries.transcript(clinic.owner, uuid4())


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
    # The call is in progress, and reception's code tied it to the patient: its own
    # database identity reads the history.
    await calls.start(now, "MZ" + uuid4().hex)
    async with tenancy.admin.begin() as database:
        await set_tenant_scope(database, clinic.tenant_id)
        await database.execute(
            update(Call)
            .where(Call.id == now.call_id)
            .values(
                verified_patient_id=clinic.patient_id,
                verification_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
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
    assert lines[0] == "Name on file: Meera Shah"
    assert lines[1].startswith("Call on ") and "moved to later the same day" in lines[1]
    assert lines[2].startswith("Coming appointment: ") and lines[2].endswith("with Dr. Rao")
    assert await CallerHistory(clinic.sessions).name(session, state) == "Meera Shah"
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
