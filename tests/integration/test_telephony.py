import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from starlette.types import Message
from starlette.websockets import WebSocket

from app.api.telephony import run_media
from app.audit.models import AuditEvent
from app.calls.models import Call, PhoneNumber
from app.calls.service import CallContext, CallService
from app.database.rls import set_tenant_scope
from app.errors import AuthorizationError, ConflictError, NotFoundError
from app.main import create_app
from app.voice.twilio import stream_token
from tests.fakes import FakeDependency
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.test_telephony import sign, voice_settings


async def seed_phone(tenancy: TenancyFixture) -> str:
    number = "+1" + str(uuid4().int)[:10]
    async with tenancy.admin.begin() as session:
        session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number=number))
    return number


async def test_incoming_retry_expires_before_stream(tenancy: TenancyFixture) -> None:
    number = await seed_phone(tenancy)
    service = CallService(tenancy.database.session_factory)
    context = await service.receive(number, "CA" + uuid4().hex, uuid4())
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(Call)
            .where(Call.id == context.call_id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=6))
        )
    with pytest.raises(ConflictError):
        await service.receive(number, context.provider_call_id, uuid4())


async def test_signed_http_lifecycle_and_isolation(tenancy: TenancyFixture) -> None:
    number = await seed_phone(tenancy)
    config = voice_settings(tenancy.settings)
    app = create_app(config, redis_factory=lambda _: FakeDependency())
    path = "/webhooks/v1/twilio/incoming"
    params = {"AccountSid": config.twilio_account_sid, "CallSid": "CA" + uuid4().hex, "To": number}
    headers = {"X-Twilio-Signature": sign(config, path, params)}
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://untrusted-host",
        ) as client,
    ):
        assert (await client.post(path, data=params)).status_code == 403
        assert (
            await client.post(path, data={**params, "To": "+15551234567"}, headers=headers)
        ).status_code == 403
        first = await client.post(path, data=params, headers=headers)
        assert first.status_code == 200 and "<Connect>" in first.text
        assert (await client.post(path, data=params, headers=headers)).text == first.text
        assert (await client.post(path + "?bad=1", data=params, headers=headers)).status_code == 400
        auth_a = {"Authorization": "Bearer " + tenancy.a.token.get_secret_value()}
        auth_b = {"Authorization": "Bearer " + tenancy.b.token.get_secret_value()}
        calls = await client.get("/api/v1/calls", headers=auth_a)
        assert calls.status_code == 200 and len(calls.json()) == 1
        identity = calls.json()[0]["id"]
        assert (await client.get("/api/v1/calls", headers=auth_b)).json() == []
        assert (await client.get("/api/v1/calls/" + identity, headers=auth_b)).status_code == 404
        status_path = "/webhooks/v1/twilio/status"
        terminal = {**params, "CallStatus": "completed"}
        for _ in range(2):
            assert (
                await client.post(
                    status_path,
                    data=terminal,
                    headers={"X-Twilio-Signature": sign(config, status_path, terminal)},
                )
            ).status_code == 204
        row = (await client.get("/api/v1/calls/" + identity, headers=auth_a)).json()
        assert row["status"] == "completed" and row["ended_at"] is not None
        assert (await client.post(path, data=params, headers=headers)).status_code == 409
    async with tenancy.database.session_factory.begin() as session:
        assert (await session.scalars(select(Call))).all() == []
        assert (await session.scalars(select(PhoneNumber))).all() == []
        await set_tenant_scope(session, tenancy.b.tenant_id)
        assert (await session.scalars(select(Call))).all() == []
    async with tenancy.admin.begin() as session:
        events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenancy.a.tenant_id, AuditEvent.action == "call_ended"
                )
            )
        ).all()
        assert len(events) == 1


@pytest.mark.parametrize("ending", ["stop", "disconnect", "invalid", "bad_token", "bad_codec"])
async def test_media_echo_and_cleanup(tenancy: TenancyFixture, ending: str) -> None:
    config = voice_settings(tenancy.settings).model_copy(update={"telephony_mode": "echo"})
    number = await seed_phone(tenancy)
    service = CallService(tenancy.database.session_factory)
    context = await service.receive(number, "CA" + uuid4().hex, uuid4())
    retry = await service.receive(number, context.provider_call_id, uuid4())
    assert retry.call_id == context.call_id
    stream_sid = "MZ" + uuid4().hex
    inbound: asyncio.Queue[Message] = asyncio.Queue()
    outbound: asyncio.Queue[Message] = asyncio.Queue()
    socket = WebSocket({"type": "websocket", "path": "/"}, inbound.get, outbound.put)
    await inbound.put({"type": "websocket.connect"})
    await socket.accept()
    await outbound.get()

    async def send(value: dict[str, Any]) -> None:
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
                "callSid": context.provider_call_id,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": 16000 if ending == "bad_codec" else 8000,
                    "channels": 1,
                },
                "customParameters": {
                    "token": "wrong" if ending == "bad_token" else stream_token(config, context)
                },
            },
        }
    )
    task = asyncio.create_task(
        run_media(socket, service, config, context.tenant_id, context.call_id)
    )
    if ending not in {"bad_token", "bad_codec"}:
        audio = base64.b64encode(b"\xff" * 160).decode()
        await send(
            {
                "event": "media",
                "sequenceNumber": "2",
                "streamSid": stream_sid,
                "media": {"track": "inbound", "payload": audio},
            }
        )
        async with asyncio.timeout(15):
            response = await outbound.get()
        assert response["type"] == "websocket.send"
        echoed = json.loads(response["text"])
        assert echoed == {"event": "media", "streamSid": stream_sid, "media": {"payload": audio}}
        if ending == "disconnect":
            await inbound.put({"type": "websocket.disconnect", "code": 1006})
        elif ending == "invalid":
            await send({"event": "media", "sequenceNumber": "2", "streamSid": stream_sid})
        else:
            await send(
                {
                    "event": "stop",
                    "sequenceNumber": "3",
                    "streamSid": stream_sid,
                    "stop": {
                        "accountSid": config.twilio_account_sid,
                        "callSid": context.provider_call_id,
                    },
                }
            )
    async with asyncio.timeout(15):
        await task
    rows = await service.list(tenancy.context(tenancy.a), 10, 0)
    row = rows[0]
    if ending in {"bad_token", "bad_codec"}:
        assert row.status == "received" and row.started_at is None
    else:
        assert (
            row.status
            == {"stop": "completed", "disconnect": "disconnected", "invalid": "failed"}[ending]
        )
        assert row.received_frames == row.sent_frames == 1 and row.ended_at is not None
        with pytest.raises(AuthorizationError):
            await service.start(context, stream_sid)


async def test_phone_assignment_rejects_tenant_sql_and_stream_cross_tenant(
    tenancy: TenancyFixture,
) -> None:
    number = await seed_phone(tenancy)
    with pytest.raises(DBAPIError):
        async with tenancy.database.session_factory.begin() as session:
            await set_tenant_scope(session, tenancy.a.tenant_id)
            session.add(PhoneNumber(tenant_id=tenancy.a.tenant_id, number="+15551234567"))
            await session.flush()
    service = CallService(tenancy.database.session_factory)
    context = await service.receive(number, "CA" + uuid4().hex, uuid4())
    with pytest.raises(NotFoundError):
        await service.start(
            CallContext(tenancy.b.tenant_id, context.call_id, context.provider_call_id, uuid4()),
            "MZ" + uuid4().hex,
        )
    await service.start(context, "MZ" + uuid4().hex)
    with pytest.raises(ConflictError):
        await service.receive(number, context.provider_call_id, uuid4())
    with pytest.raises(AuthorizationError):
        await service.start(context, "MZ" + uuid4().hex)
