import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.agents.appointment_flow import BookInput, CancelInput, RescheduleInput, caller_context
from app.agents.appointment_tools import appointment_tools
from app.agents.context import ALL_TOOLS, AgentContext
from app.agents.runtime import AgentRuntime
from app.agents.schemas import ToolCall
from app.agents.tools import ToolRegistry
from app.appointments.models import Appointment
from app.appointments.service import AppointmentService
from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext
from app.calls.access import CallPatientAccess, IssuePatientAccess
from app.calls.models import Call, PhoneNumber
from app.calls.service import CallContext, CallService
from app.calls.verification import VerificationPolicy
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, FlowStep
from app.core.database.session import Database
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import AuthorizationError, ConflictError, NotFoundError
from app.events.models import OutboxEvent
from app.patients.schemas import UpdatePatientVerification
from app.patients.service import PatientService
from app.tenants.service import TenantService
from tests.integration.test_domain import seed
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.test_agent_runtime import ScriptedProvider, decision


async def setup_call(tenancy: TenancyFixture):
    domain = await seed(UnitOfWork(tenancy.database.session_factory), tenancy.context(tenancy.a))
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
    calls = CallService(tenancy.database.session_factory)
    call = await calls.receive(number, "CA" + uuid4().hex, uuid4())
    await calls.start(call, "MZ" + uuid4().hex)
    state = ConversationStateManager.create_initial_state(
        call.call_id, call.tenant_id, call.provider_call_id
    )
    context = AgentContext(call, ALL_TOOLS)
    registry, flow = appointment_tools(tenancy.database.session_factory)
    return domain, context, state, registry, flow


async def identify(
    tenancy: TenancyFixture,
    patient_id: UUID,
    context: AgentContext,
    state: ConversationState,
    registry: ToolRegistry,
) -> None:
    access = CallPatientAccess(tenancy.database.session_factory)
    code = await access.issue(
        tenancy.context(tenancy.a),
        context.call.call_id,
        IssuePatientAccess(patient_id=patient_id, verification_attested=True),
    )
    result = await registry.execute(
        context,
        state,
        ToolCall(name="identify_patient", arguments_json=json.dumps({"access_code": code.code})),
    )
    assert result["status"] == "identified"


async def count_appointments(tenancy: TenancyFixture) -> int:
    async with tenancy.admin.begin() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.tenant_id == tenancy.a.tenant_id)
            )
            or 0
        )


async def test_booking_changed_time_confirmation_replay_and_lifecycle(tenancy: TenancyFixture):
    domain, context, state, registry, flow = await setup_call(tenancy)
    await identify(tenancy, domain.patient_id, context, state, registry)
    args = {"provider_id": str(domain.provider_id), "starts_at": domain.start.isoformat()}
    later = {**args, "starts_at": (domain.start + timedelta(minutes=30)).isoformat()}
    provider = ScriptedProvider(
        [
            decision("book_appointment", json.dumps(args)),
            decision("book_appointment", json.dumps(later)),
        ]
    )
    agent = AgentRuntime(provider, registry, appointment_flow=flow)
    first = await agent.respond(context, state, "Book the first slot")
    assert "Say yes" in first.response and await count_appointments(tenancy) == 0
    assert state.appointment_operation is not None
    first_key = state.appointment_operation.key
    ConversationStateManager.process_caller_utterance(state, "Actually thirty minutes later")
    second = await agent.respond(context, state, "Actually thirty minutes later")
    assert "Say yes" in second.response and state.appointment_operation.key != first_key
    assert await count_appointments(tenancy) == 0
    pending = state.appointment_operation.model_copy(deep=True)
    ConversationStateManager.process_caller_utterance(state, "Yes, book that please")
    booked = await agent.respond(context, state, "Yes, book that please")
    assert "is booked" in booked.response and state.active_flow_state.step == FlowStep.COMPLETED
    assert len(provider.requests) == 2  # Confirmation never reaches the model.
    assert await count_appointments(tenancy) == 1
    # Simulate losing the committed response during interruption. Replay is safe.
    pending.status = "executing"
    state.appointment_operation = pending
    replay = await agent.respond(context, state, "Did that finish?")
    assert replay.response == booked.response and await count_appointments(tenancy) == 1
    rows = await flow.appointments.list(caller_context(context, state), 20, 0)
    assert rows[0].starts_at == domain.start + timedelta(minutes=30)
    appointment = rows[0]
    await flow.propose(
        context,
        state,
        "reschedule_appointment",
        RescheduleInput(
            appointment_id=appointment.id, starts_at=domain.start + timedelta(minutes=60)
        ),
    )
    result = await agent.respond(context, state, "yes")
    assert "moved" in result.response
    await flow.propose(
        context, state, "cancel_appointment", CancelInput(appointment_id=appointment.id)
    )
    result = await agent.respond(context, state, "yes")
    assert "cancelled" in result.response
    async with tenancy.admin.begin() as session:
        saved = await session.get(Appointment, appointment.id)
        assert saved is not None
        assert saved.status == "cancelled" and saved.version == 3
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == appointment.id)
            )
            == 3
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.call_id == context.call.call_id,
                    AuditEvent.action.in_(
                        ["appointment_created", "appointment_cancelled", "appointment_rescheduled"]
                    ),
                )
            )
            == 3
        )


@pytest.mark.parametrize("reply", ["no", "yes but tomorrow", "yes don't book", "stop"])
async def test_ambiguous_or_negative_confirmation_never_mutates(
    tenancy: TenancyFixture, reply: str
):
    domain, context, state, registry, flow = await setup_call(tenancy)
    await identify(tenancy, domain.patient_id, context, state, registry)
    await flow.propose(
        context,
        state,
        "book_appointment",
        BookInput(provider_id=domain.provider_id, starts_at=domain.start),
    )
    ConversationStateManager.process_caller_utterance(state, reply)
    await flow.before_turn(context, state, reply)
    assert state.appointment_operation is None and await count_appointments(tenancy) == 0


async def test_expired_confirmation_and_slot_taken(tenancy: TenancyFixture):
    domain, context, state, registry, flow = await setup_call(tenancy)
    await identify(tenancy, domain.patient_id, context, state, registry)
    payload = BookInput(provider_id=domain.provider_id, starts_at=domain.start)
    await flow.propose(context, state, "book_appointment", payload)
    state.confirmation_state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await flow.before_turn(context, state, "yes") is None
    assert await count_appointments(tenancy) == 0
    await flow.propose(context, state, "book_appointment", payload)
    await flow.appointments.book(tenancy.context(tenancy.a), domain.booking(), "occupied")
    result = await flow.before_turn(context, state, "yes")
    assert result is not None and "not available" in result.response
    assert await count_appointments(tenancy) == 1


async def test_access_scope_lockout_and_closed_call(tenancy: TenancyFixture):
    domain, context, _state, _registry, flow = await setup_call(tenancy)
    access = flow.access
    command = IssuePatientAccess(patient_id=domain.patient_id, verification_attested=True)
    with pytest.raises(AuthorizationError):
        await access.issue(tenancy.context(tenancy.b), context.call.call_id, command)
    with pytest.raises(AuthorizationError):
        await access.issue(
            tenancy.context(tenancy.a),
            context.call.call_id,
            command.model_copy(update={"verification_attested": False}),
        )
    code = await access.issue(tenancy.context(tenancy.a), context.call.call_id, command)
    wrong = "00000000" if code.code != "00000000" else "11111111"
    assert await access.identify(context.call, wrong) is None
    assert await access.identify(context.call, wrong) is None
    assert await access.identify(context.call, wrong) is None
    assert await access.identify(context.call, code.code) is None
    with pytest.raises(ConflictError):
        await access.issue(tenancy.context(tenancy.a), context.call.call_id, command)
    await CallService(tenancy.database.session_factory).finish(context.call, "completed", "test")
    with pytest.raises(AuthorizationError):
        await access.identify(context.call, code.code)


async def test_configured_verification_blocks_reads_and_revokes_old_access(
    tenancy: TenancyFixture,
) -> None:
    domain, context, state, registry, flow = await setup_call(tenancy)
    admin = tenancy.context(tenancy.a)
    unauthenticated = caller_context(context, state)
    with pytest.raises(AuthorizationError):
        await flow.appointments.list(unauthenticated, 10, 0)
    await PatientService(domain.uow).set_verification(
        admin,
        domain.patient_id,
        UpdatePatientVerification(date_of_birth=date(1990, 4, 9), account_identifier="PAT-137"),
    )
    await TenantService(domain.uow).set_verification_policy(
        admin,
        VerificationPolicy(
            required_factors=["reception_attestation", "date_of_birth", "account_identifier"],
            access_ttl_minutes=5,
            max_code_attempts=2,
        ),
    )
    command = IssuePatientAccess(patient_id=domain.patient_id, verification_attested=True)
    with pytest.raises(AuthorizationError):
        await flow.access.issue(admin, context.call.call_id, command)
    with pytest.raises(AuthorizationError):
        await flow.access.issue(
            admin,
            context.call.call_id,
            command.model_copy(
                update={"date_of_birth": date(1990, 4, 9), "account_identifier": "BAD"}
            ),
        )
    code = await flow.access.issue(
        admin,
        context.call.call_id,
        IssuePatientAccess(
            patient_id=domain.patient_id,
            verification_attested=True,
            date_of_birth=date(1990, 4, 9),
            account_identifier="PAT-137",
        ),
    )
    result = await registry.execute(
        context,
        state,
        ToolCall(name="identify_patient", arguments_json=json.dumps({"access_code": code.code})),
    )
    assert result["status"] == "identified" and state.identity_state.is_verified
    assert await flow.appointments.list(caller_context(context, state), 10, 0) == []
    async with tenancy.admin.begin() as session:
        assert (
            await session.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.tenant_id == tenancy.a.tenant_id,
                    AuditEvent.call_id == context.call.call_id,
                    AuditEvent.action == "appointments_viewed",
                )
            )
            is not None
        )
    await PatientService(domain.uow).set_verification(
        admin,
        domain.patient_id,
        UpdatePatientVerification(date_of_birth=date(1990, 4, 9), account_identifier="PAT-138"),
    )
    with pytest.raises(AuthorizationError):
        await flow.appointments.list(caller_context(context, state), 10, 0)
    replacement = await flow.access.issue(
        admin,
        context.call.call_id,
        IssuePatientAccess(
            patient_id=domain.patient_id,
            verification_attested=True,
            date_of_birth=date(1990, 4, 9),
            account_identifier="PAT-138",
        ),
    )
    assert (
        await registry.execute(
            context,
            state,
            ToolCall(
                name="identify_patient",
                arguments_json=json.dumps({"access_code": replacement.code}),
            ),
        )
    )["status"] == "identified"
    assert await flow.appointments.list(caller_context(context, state), 10, 0) == []
    await TenantService(domain.uow).set_verification_policy(admin, VerificationPolicy())
    with pytest.raises(AuthorizationError):
        await flow.appointments.list(caller_context(context, state), 10, 0)


async def test_caller_cannot_forge_identity_or_access_another_patient(tenancy: TenancyFixture):
    domain, context, state, registry, flow = await setup_call(tenancy)
    forged = RequestContext(
        uuid4(),
        context.call.tenant_id,
        None,
        ActorType.CALLER,
        call_id=context.call.call_id,
        patient_id=domain.patient_id,
        provider_call_id=context.call.provider_call_id,
    )
    with pytest.raises(AuthorizationError):
        await flow.appointments.book(forged, domain.booking(), "forged")
    await identify(tenancy, domain.patient_id, context, state, registry)
    other = await seed(domain.uow, tenancy.context(tenancy.a))
    appointment = await flow.appointments.book(tenancy.context(tenancy.a), other.booking(), "other")
    with pytest.raises(NotFoundError):
        await flow.propose(
            context, state, "cancel_appointment", CancelInput(appointment_id=appointment.id)
        )
    with pytest.raises(AuthorizationError):
        await flow.appointments.book(
            caller_context(context, state), other.booking(), "other-forged"
        )
    cross = CallContext(
        tenancy.b.tenant_id, context.call.call_id, context.call.provider_call_id, uuid4()
    )
    with pytest.raises(AuthorizationError):
        await flow.access.identify(cross, "00000000")


async def test_concurrent_replay_has_one_appointment(tenancy: TenancyFixture):
    domain, context, state, registry, _flow = await setup_call(tenancy)
    await identify(tenancy, domain.patient_id, context, state, registry)
    request = caller_context(context, state)
    database = Database(tenancy.settings.model_copy(update={"db_pool_size": 4}))
    try:
        appointments = AppointmentService(UnitOfWork(database.session_factory))
        results = await asyncio.gather(
            *[appointments.book(request, domain.booking(), "same-call-operation") for _ in range(2)]
        )
    finally:
        await database.close()
    assert results[0].id == results[1].id and await count_appointments(tenancy) == 1


async def test_access_code_expiry_and_single_patient_binding(tenancy: TenancyFixture):
    domain, context, state, registry, flow = await setup_call(tenancy)
    code = await flow.access.issue(
        tenancy.context(tenancy.a),
        context.call.call_id,
        IssuePatientAccess(patient_id=domain.patient_id, verification_attested=True),
    )
    async with tenancy.admin.begin() as session:
        call = await session.get(Call, context.call.call_id)
        assert call is not None
        call.access_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await flow.access.identify(context.call, code.code) is None
    await identify(tenancy, domain.patient_id, context, state, registry)
    with pytest.raises(ConflictError):
        await flow.access.issue(
            tenancy.context(tenancy.a),
            context.call.call_id,
            IssuePatientAccess(patient_id=domain.patient_id, verification_attested=True),
        )


async def test_reception_access_endpoint_requires_tenant_admin(tenancy: TenancyFixture):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from tests.fakes import FakeDependency

    domain, context, _state, _registry, _flow = await setup_call(tenancy)
    app = create_app(tenancy.settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        path = f"/api/v1/calls/{context.call.call_id}/patient-access"
        body = {"patient_id": str(domain.patient_id), "verification_attested": True}
        assert (await client.post(path, json=body)).status_code == 401
        other = {"Authorization": f"Bearer {tenancy.b.token.get_secret_value()}"}
        assert (await client.post(path, headers=other, json=body)).status_code == 403
        headers = {"Authorization": f"Bearer {tenancy.a.token.get_secret_value()}"}
        response = await client.post(path, headers=headers, json=body)
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        assert len(response.json()["code"]) == 8


async def test_standalone_access_code_bypasses_model_and_is_redacted(tenancy: TenancyFixture):
    domain, context, state, registry, flow = await setup_call(tenancy)
    code = await flow.access.issue(
        tenancy.context(tenancy.a),
        context.call.call_id,
        IssuePatientAccess(patient_id=domain.patient_id, verification_attested=True),
    )
    provider = ScriptedProvider([])
    runtime = AgentRuntime(provider, registry, appointment_flow=flow)
    ConversationStateManager.process_caller_utterance(state, code.code)
    assert state.last_caller_utterance == "[call access code]"
    result = await runtime.respond(context, state, code.code)
    assert result.status == "completed" and result.tools_used == ["identify_patient"]
    assert state.identity_state.patient_id == domain.patient_id
    assert provider.requests == []


async def test_booking_through_voice_websocket_without_external_providers(
    tenancy: TenancyFixture,
) -> None:
    import base64
    from unittest.mock import AsyncMock, MagicMock

    from pydantic import SecretStr
    from starlette.types import Message
    from starlette.websockets import WebSocket

    from app.api.telephony import run_media
    from app.conversation.store import MemoryConversationStateStore
    from app.voice.stt.base import TranscriptionResult
    from app.voice.twilio import stream_token
    from tests.fake_stt import MockSTTProvider
    from tests.fake_tts import MockTTSProvider

    domain = await seed(UnitOfWork(tenancy.database.session_factory), tenancy.context(tenancy.a))
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
    calls = CallService(tenancy.database.session_factory)
    call = await calls.receive(number, "CA" + uuid4().hex, uuid4())
    stream_sid = "MZ" + uuid4().hex
    await calls.start(call, stream_sid)
    code = await CallPatientAccess(tenancy.database.session_factory).issue(
        tenancy.context(tenancy.a),
        call.call_id,
        IssuePatientAccess(patient_id=domain.patient_id, verification_attested=True),
    )
    scripted = ScriptedProvider(
        [
            decision(
                "book_appointment",
                json.dumps(
                    {
                        "provider_id": str(domain.provider_id),
                        "starts_at": domain.start.isoformat(),
                    }
                ),
            )
        ]
    )
    config = tenancy.settings.model_copy(
        update={
            "twilio_account_sid": "AC" + "1" * 32,
            "twilio_auth_token": SecretStr("test-token"),
            "telephony_public_base_url": "https://voice.example.test",
        }
    )
    store = MemoryConversationStateStore()
    app = MagicMock()
    app.state.resources = None
    app.state.telemetry = None
    app.state.stt_provider = MockSTTProvider(
        results=[
            TranscriptionResult(text=code.code, is_final=True),
            TranscriptionResult(text="Book the appointment", is_final=True),
            TranscriptionResult(text="Yes, book that please", is_final=True),
        ]
    )
    app.state.tts_provider = MockTTSProvider(total_chunks=1)
    app.state.llm_provider = scripted
    app.state.conversation_store = store
    service = MagicMock()
    service.sessions = tenancy.database.session_factory
    service.start = AsyncMock(return_value=call)
    service.finish = AsyncMock(side_effect=calls.finish)
    service.transfer_number = AsyncMock(return_value=None)
    service.operational_configuration = AsyncMock(side_effect=calls.operational_configuration)
    service.language_policy = AsyncMock(side_effect=calls.language_policy)
    inbound: asyncio.Queue[Message] = asyncio.Queue()
    outbound: asyncio.Queue[Message] = asyncio.Queue()
    socket = WebSocket({"type": "websocket", "path": "/", "app": app}, inbound.get, outbound.put)
    await inbound.put({"type": "websocket.connect"})
    await socket.accept()
    await outbound.get()

    async def send(value: dict[str, object]) -> None:
        await inbound.put({"type": "websocket.receive", "text": json.dumps(value)})

    await send({"event": "connected", "protocol": "Call", "version": "1.0.0"})
    await send(
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": stream_sid,
            "start": {
                "streamSid": stream_sid,
                "accountSid": config.twilio_account_sid,
                "callSid": call.provider_call_id,
                "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"token": stream_token(config, call)},
            },
        }
    )
    task = asyncio.create_task(run_media(socket, service, config, call.tenant_id, call.call_id))

    async def media(sequence: int) -> None:
        await send(
            {
                "event": "media",
                "sequenceNumber": str(sequence),
                "streamSid": stream_sid,
                "media": {
                    "track": "inbound",
                    "payload": base64.b64encode(bytes([255]) * 160).decode(),
                },
            }
        )

    async def state_until(predicate: Callable[[ConversationState], bool]) -> ConversationState:
        async with asyncio.timeout(10):
            while True:
                state = await store.get(call.tenant_id, call.call_id)
                if state is not None and predicate(state):
                    return state
                await asyncio.sleep(0.01)

    await media(2)
    identified = await state_until(lambda state: state.identity_state.is_verified)
    assert identified.last_caller_utterance == "[call access code]"
    await media(3)
    proposed = await state_until(lambda state: state.appointment_operation is not None)
    assert proposed.awaiting_confirmation and await count_appointments(tenancy) == 0
    await media(4)
    await state_until(
        lambda state: (
            state.appointment_operation is not None
            and state.appointment_operation.status == "completed"
        )
    )
    assert await count_appointments(tenancy) == 1
    assert len(scripted.requests) == 1
    await send(
        {
            "event": "stop",
            "sequenceNumber": "5",
            "streamSid": stream_sid,
            "stop": {"accountSid": config.twilio_account_sid, "callSid": call.provider_call_id},
        }
    )
    async with asyncio.timeout(10):
        await task
    saved = await store.get(call.tenant_id, call.call_id)
    assert saved is not None and saved.call_state.status.value == "completed"
