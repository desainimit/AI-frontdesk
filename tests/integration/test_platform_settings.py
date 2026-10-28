"""Provider accounts and product settings saved by a platform administrator."""

import base64
import json
import secrets
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select, text

from app.calls.models import PhoneNumber
from app.core.database.rls import set_tenant_scope
from app.core.settings import Settings
from app.integrations.verifiers import Verification
from app.main import create_app
from app.platform.connections import refresh_platform
from app.platform.models import PlatformConnection
from tests.fakes import FakeDependency
from tests.integration.identity_harness import IdentityHarness
from tests.integration.test_integrations import FakeVerifier
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy

ACCOUNT = "AC" + "a" * 32
TOKEN = "b" * 32
NUMBER_SID = "PN" + "c" * 32
CALLS_ADDRESS = "https://calls.example.test"


@pytest.fixture
async def platform(tenancy: TenancyFixture) -> AsyncGenerator[Settings]:
    """Settings with a keyring and public address, and a clean platform afterwards."""
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    settings = tenancy.settings.model_copy(
        update={
            "secret_encryption_keys": SecretStr(json.dumps({"k1": key})),
            "secret_encryption_active_key": "k1",
            "telephony_public_base_url": CALLS_ADDRESS,
            "stream_token_key": SecretStr(secrets.token_urlsafe(48)),
        }
    )
    try:
        yield settings
    finally:
        # These tables are shared by every workspace, so nothing may be left behind.
        async with tenancy.admin.begin() as session:
            await session.execute(delete(PlatformConnection))
            await session.execute(
                text("UPDATE platform_settings SET session_minutes = 30, speaking_speed = 'normal'")
            )


async def test_administrator_saves_verifies_and_removes_a_provider_account(
    tenancy: TenancyFixture, platform: Settings
) -> None:
    admin = await IdentityHarness(tenancy).admin()
    app = create_app(platform, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    verifier = FakeVerifier()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test/api/v1/platform/",
            headers=admin.headers,
        ) as client,
    ):
        app.state.integration_verifier = verifier
        listed = (await client.get("connections")).json()
        assert [item["purpose"] for item in listed] == [
            "email",
            "telephony",
            "stt",
            "tts",
            "llm",
            "google_calendar",
            "microsoft_calendar",
        ]
        assert all(item["status"] == "not_configured" for item in listed)
        assert not platform.platform_twilio

        body = {
            "provider": "twilio",
            "account_identifier": ACCOUNT,
            "secrets": {"auth_token": TOKEN},
        }
        saved = await client.put("connections/telephony", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["status"] == "verified"
        assert saved.json()["secret_hint"] == "ends in bbbb" and TOKEN not in saved.text
        # The running application uses the saved account at once.
        assert platform.platform_twilio and platform.twilio_account_sid == ACCOUNT
        assert platform.twilio_auth_token.get_secret_value() == TOKEN
        checklist = {item["key"]: item["done"] for item in (await client.get("checklist")).json()}
        assert checklist["telephony"] and checklist["call_address"] and not checklist["email"]
        # Another process (here: fresh settings) picks it up from the database.
        elsewhere = tenancy.settings.model_copy(
            update={
                "secret_encryption_keys": platform.secret_encryption_keys,
                "secret_encryption_active_key": "k1",
            }
        )
        assert not elsewhere.platform_twilio
        assert await refresh_platform(elsewhere, tenancy.database.session_factory)
        assert elsewhere.twilio_account_sid == ACCOUNT
        async with tenancy.admin.begin() as session:
            row = await session.scalar(select(PlatformConnection))
            assert row is not None and TOKEN.encode() not in row.ciphertext

        # Changing a detail keeps the saved secret; a rejected secret is reported.
        verifier.outcome = Verification("invalid", "credentials_rejected")
        rejected = await client.put(
            "connections/stt",
            json={"provider": "deepgram", "config": {}, "secrets": {"api_key": "k" * 32}},
        )
        assert rejected.json()["status"] == "invalid"
        assert "did not accept" in rejected.json()["problem"]
        assert not platform.deepgram_api_key.get_secret_value()
        bad = await client.put("connections/telephony", json={"provider": "twilio", "secrets": {}})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_connection"

        product = (await client.get("settings")).json()
        changed = await client.put("settings", json={**product, "session_minutes": 45})
        assert changed.status_code == 200
        assert platform.platform_session_minutes == 45
        # The AI model choices come from the catalog, never from a typed list.
        assert "allowed_llm_models" not in product
        wrong = await client.put(
            "connections/llm",
            json={
                "provider": "openrouter",
                "config": {"voice_model": "google/gemini-3.8-flash"},
                "secrets": {"api_key": "k" * 32},
            },
        )
        assert wrong.status_code == 422
        assert wrong.json()["error"]["fields"][0]["field"] == "config.voice_model"

        assert (await client.delete("connections/telephony")).status_code == 204
        assert not platform.platform_twilio
        # Only platform administrators reach any of this.
        member = await client.get("connections", headers=tenancy.a.headers)
        assert member.status_code == 403


async def test_assigning_a_number_points_its_webhooks_here(
    tenancy: TenancyFixture, platform: Settings
) -> None:
    admin = await IdentityHarness(tenancy).admin()
    number = "+1" + str(uuid4().int)[:10]
    updates: list[dict[str, list[str]]] = []

    def twilio(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.twilio.com"
        if request.method == "POST":
            assert request.url.path.endswith(f"/IncomingPhoneNumbers/{NUMBER_SID}.json")
            updates.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={})
        owned: list[dict[str, Any]] = [
            {"sid": NUMBER_SID, "phone_number": number, "account_sid": ACCOUNT}
        ]
        return httpx.Response(200, json={"incoming_phone_numbers": owned})

    app = create_app(platform, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test/api/v1/platform/",
            headers=admin.headers,
        ) as client,
    ):
        app.state.integration_verifier = FakeVerifier()
        app.state.provider_transport = httpx.MockTransport(twilio)
        assert (await client.get("phone-numbers")).status_code == 409  # no phone service yet
        body = {
            "provider": "twilio",
            "account_identifier": ACCOUNT,
            "secrets": {"auth_token": TOKEN},
        }
        assert (await client.put("connections/telephony", json=body)).json()["status"] == "verified"
        assert (await client.get("phone-numbers")).json() == [
            {
                "number": number,
                "tenant_id": None,
                "tenant_name": None,
                "is_active": False,
                "webhooks_configured": None,
            }
        ]
        assigned = await client.post(
            "phone-numbers", json={"tenant_id": str(tenancy.a.tenant_id), "number": number}
        )
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["webhooks_configured"] is True
        assert updates[0]["VoiceUrl"] == [f"{CALLS_ADDRESS}/webhooks/v1/twilio/incoming"]
        assert updates[0]["StatusCallback"] == [f"{CALLS_ADDRESS}/webhooks/v1/twilio/status"]
        assert updates[0]["SmsUrl"] == [f"{CALLS_ADDRESS}/webhooks/v1/twilio/sms"]
        # After the public address changes, the same number is pointed here again.
        again = await client.post(f"phone-numbers/{number}/address")
        assert again.status_code == 200 and again.json()["webhooks_configured"] is True
        assert len(updates) == 2 and updates[1]["VoiceUrl"] == updates[0]["VoiceUrl"]
        listed = (await client.get("phone-numbers")).json()
        assert listed[0]["tenant_name"] == "Tenant A" and listed[0]["is_active"]
        # A number serves one clinic; one the account does not own is refused.
        taken = await client.post(
            "phone-numbers", json={"tenant_id": str(tenancy.b.tenant_id), "number": number}
        )
        assert taken.status_code == 409 and taken.json()["error"]["code"] == "number_taken"
        foreign = await client.post(
            "phone-numbers", json={"tenant_id": str(tenancy.a.tenant_id), "number": "+15550001111"}
        )
        assert foreign.status_code == 422
        assert (await client.delete(f"phone-numbers/{number}")).status_code == 204
    async with tenancy.admin.begin() as session:
        await set_tenant_scope(session, tenancy.a.tenant_id)
        row = await session.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
        assert row is not None and not row.is_active and row.provider_number_sid == NUMBER_SID
