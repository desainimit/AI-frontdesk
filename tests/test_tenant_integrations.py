"""Provider catalog, credential checks, stream tokens and settings; no database needed."""

import base64
import json
import secrets
from uuid import uuid4

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr, ValidationError

from app.calls.service import CallContext
from app.config.settings import Settings
from app.integrations.catalog import (
    InvalidIntegrationError,
    catalog_entries,
    spec_for,
    validate_config,
    validate_secrets,
)
from app.integrations.resolver import TenantIntegrationResolver
from app.integrations.service import SubmitCredentials
from app.integrations.verifiers import HttpProviderVerifier, ProviderUnavailableError
from app.security.encryption import SecretCipher
from app.voice.circuit import CircuitRegistry
from app.voice.twilio import (
    TwilioCredentials,
    matching_credentials,
    stream_token,
    stream_token_valid,
)

SID = "AC" + "a" * 32
TOKEN = "b" * 32


def keyring() -> SecretStr:
    return SecretStr(json.dumps({"k1": base64.b64encode(secrets.token_bytes(32)).decode()}))


def test_catalog_accepts_only_allowlisted_configuration() -> None:
    deepgram = spec_for("stt", "deepgram")
    assert validate_config(deepgram, {}) == {"model": "nova-3", "language": "multi"}
    for config in (
        {"model": "nova-2", "language": "multi"},  # Multilingual needs nova-3.
        {"base_url": "https://attacker.example"},
        {"model": "nova-3", "headers": {"X": "y"}},
    ):
        with pytest.raises(InvalidIntegrationError):
            validate_config(deepgram, config)
    azure = spec_for("tts", "azure_speech")
    with pytest.raises(InvalidIntegrationError):
        validate_config(azure, {"region": "eastus.attacker.example"})
    with pytest.raises(InvalidIntegrationError):
        validate_config(spec_for("tts", "elevenlabs"), {"voice_id": "../../v1/user"})
    for purpose, provider in (("stt", "twilio"), ("telephony", "deepgram"), ("sms", "twilio")):
        with pytest.raises(InvalidIntegrationError):
            spec_for(purpose, provider)
    entries = catalog_entries(("gpt-4o-mini",))
    llm = next(item for item in entries if item.purpose == "llm")
    assert llm.config_schema["properties"]["model"]["enum"] == ["gpt-4o-mini"]
    twilio = next(item for item in entries if item.provider == "twilio")
    assert twilio.requires_account_identifier and twilio.required_secret_fields == ["auth_token"]


def test_secret_validation_never_echoes_the_submitted_value() -> None:
    twilio = spec_for("telephony", "twilio")
    assert validate_secrets(twilio, {"auth_token": TOKEN}) == {"auth_token": TOKEN}
    leaked = "not-a-valid-token-but-secret"
    for secrets_ in (
        {"auth_token": leaked},
        {"auth_token": TOKEN, "api_key_sid": "SK" + "c" * 32},  # Secret half missing.
        {"auth_token": TOKEN, "unexpected": leaked},
    ):
        with pytest.raises(InvalidIntegrationError) as error:
            validate_secrets(twilio, secrets_)
        assert leaked not in str(error.value)
    with pytest.raises(InvalidIntegrationError):
        validate_secrets(spec_for("stt", "deepgram"), {"api_key": "has a space in it 1234"})
    command = SubmitCredentials(revision=1, secrets={"auth_token": leaked})
    assert leaked not in repr(command)
    with pytest.raises(ValidationError) as rejected:
        SubmitCredentials.model_validate({"revision": 1, "secrets": {"auth_token": [leaked]}})
    assert leaked not in str(rejected.value)


def test_cipher_binds_ciphertext_to_its_context_and_key_version() -> None:
    ring = keyring()
    cipher = SecretCipher(ring, "k1")
    key_id, first = cipher.encrypt(b"secret", "integration:t1:c1:twilio:telephony:1")
    _, second = cipher.encrypt(b"secret", "integration:t1:c1:twilio:telephony:1")
    assert first != second and b"secret" not in first  # Fresh nonce every time.
    assert cipher.decrypt(key_id, first, "integration:t1:c1:twilio:telephony:1") == b"secret"
    with pytest.raises(InvalidTag):
        cipher.decrypt(key_id, first, "integration:t2:c1:twilio:telephony:1")
    with pytest.raises(ValueError):
        cipher.decrypt("retired-key", first, "integration:t1:c1:twilio:telephony:1")
    assert cipher.fingerprint(b"secret", "a") == cipher.fingerprint(b"secret", "a")
    assert cipher.fingerprint(b"secret", "a") != cipher.fingerprint(b"secret", "b")
    assert cipher.fingerprint(b"secret", "a") != SecretCipher(keyring(), "k1").fingerprint(
        b"secret", "a"
    )
    for bad in (SecretStr(""), SecretStr("{}"), SecretStr(json.dumps({"k1": "c2hvcnQ="}))):
        with pytest.raises(ValueError):
            SecretCipher(bad, "k1")


async def test_verifier_classifies_provider_answers_and_uses_fixed_hosts() -> None:
    requests: list[httpx.Request] = []
    status = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.twilio.com" and status["code"] == 200:
            if request.url.path.endswith("IncomingPhoneNumbers.json"):
                return httpx.Response(
                    200,
                    json={
                        "incoming_phone_numbers": [
                            {
                                "sid": "PN" + "1" * 32,
                                "phone_number": "+15551230001",
                                "account_sid": SID,
                            },
                            # Another account's resource and a malformed row are ignored.
                            {
                                "sid": "PN" + "2" * 32,
                                "phone_number": "+15551230002",
                                "account_sid": "AC" + "f" * 32,
                            },
                            {"sid": "bad", "phone_number": "+15551230003", "account_sid": SID},
                        ]
                    },
                )
            return httpx.Response(200, json={"sid": SID, "status": "active"})
        return httpx.Response(status["code"], json={})

    verifier = HttpProviderVerifier(2, transport=httpx.MockTransport(handler))
    cases: tuple[tuple[str, str, str | None, dict[str, str], dict[str, str], str], ...] = (
        ("telephony", "twilio", SID, {}, {"auth_token": TOKEN}, "api.twilio.com"),
        ("stt", "deepgram", None, {}, {"api_key": "k" * 20}, "api.deepgram.com"),
        (
            "tts",
            "elevenlabs",
            None,
            {"voice_id": "v" * 20},
            {"api_key": "k" * 20},
            "api.elevenlabs.io",
        ),
        (
            "stt",
            "azure_speech",
            None,
            {"region": "eastus"},
            {"speech_key": "k" * 20},
            "eastus.api.cognitive.microsoft.com",
        ),
        ("llm", "openai", None, {"model": "gpt-4o-mini"}, {"api_key": "k" * 20}, "api.openai.com"),
    )
    for purpose, provider, account, config, secrets_, host in cases:
        requests.clear()
        status["code"] = 200
        assert (await verifier.verify(purpose, provider, account, config, secrets_)).outcome == (
            "verified"
        )
        assert {request.url.host for request in requests} == {host}
        assert all(request.url.scheme == "https" for request in requests)
        status["code"] = 401
        rejected = await verifier.verify(purpose, provider, account, config, secrets_)
        assert (rejected.outcome, rejected.code) == ("invalid", "credentials_rejected")
        status["code"] = 503
        down = await verifier.verify(purpose, provider, account, config, secrets_)
        assert down.outcome == "unavailable"
        status["code"] = 302  # Redirects are never followed to another host.
        assert (
            await verifier.verify(purpose, provider, account, config, secrets_)
        ).outcome == "unavailable"

    status["code"] = 404
    missing = await verifier.verify("llm", "openai", None, {"model": "x"}, {"api_key": "k" * 20})
    assert (missing.outcome, missing.code) == ("invalid", "model_unavailable")
    status["code"] = 200
    numbers = await verifier.twilio_numbers(SID, {"auth_token": TOKEN})
    assert [(item.sid, item.number) for item in numbers] == [("PN" + "1" * 32, "+15551230001")]
    status["code"] = 500
    with pytest.raises(ProviderUnavailableError):
        await verifier.twilio_numbers(SID, {"auth_token": TOKEN})

    def offline(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to " + TOKEN)

    unreachable = HttpProviderVerifier(2, transport=httpx.MockTransport(offline))
    result = await unreachable.verify("telephony", "twilio", SID, {}, {"auth_token": TOKEN})
    assert result.outcome == "unavailable" and TOKEN not in result.code


def tenant_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "telephony_enabled": True,
            "telephony_public_base_url": "https://voice.example.test",
            "twilio_account_sid": SID,
            "twilio_auth_token": SecretStr(TOKEN),
            "stream_token_key": SecretStr(secrets.token_urlsafe(48)),
        }
    )


def test_stream_token_is_bound_to_call_connection_and_expiry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tenant_settings(settings)
    context = CallContext(uuid4(), uuid4(), "CA" + "1" * 32, uuid4())
    connection = TwilioCredentials(
        SID, SecretStr(TOKEN), integration_id=uuid4(), version_id=uuid4()
    )
    token = stream_token(config, context, connection)
    assert token.startswith("v2.") and TOKEN not in token
    assert stream_token_valid(config, context, token, connection)
    other_call = CallContext(context.tenant_id, uuid4(), context.provider_call_id, uuid4())
    other_tenant = CallContext(uuid4(), context.call_id, context.provider_call_id, uuid4())
    other_connection = TwilioCredentials(SID, SecretStr(TOKEN), integration_id=uuid4())
    other_account = TwilioCredentials(
        "AC" + "c" * 32, SecretStr(TOKEN), integration_id=connection.integration_id
    )
    assert not stream_token_valid(config, other_call, token, connection)
    assert not stream_token_valid(config, other_tenant, token, connection)
    assert not stream_token_valid(config, context, token, other_connection)
    assert not stream_token_valid(config, context, token, other_account)
    # Knowing the provider's secret does not let anyone mint a stream token.
    rekeyed = config.model_copy(update={"stream_token_key": SecretStr(TOKEN * 2)})
    assert not stream_token_valid(rekeyed, context, token, connection)
    for forged in ("", "v2.abc.def", "v1." + token[3:], token + "0", None, 5, "v2.9999999999.00"):
        assert not stream_token_valid(config, context, forged, connection)
    version, expires, mac = token.split(".")
    assert not stream_token_valid(
        config, context, f"{version}.{int(expires) + 60}.{mac}", connection
    )
    monkeypatch.setattr("app.voice.twilio.time.time", lambda: int(expires) + 1)
    assert not stream_token_valid(config, context, token, connection)
    # Legacy platform-account calls keep their existing token contract.
    legacy = stream_token(config, context)
    assert stream_token_valid(config, context, legacy)
    assert not stream_token_valid(config, context, legacy, connection)


def test_signature_matches_only_the_account_that_signed(settings: Settings) -> None:
    from twilio.request_validator import RequestValidator  # pyright: ignore[reportMissingTypeStubs]

    config = tenant_settings(settings)
    path, params = "/webhooks/v1/twilio/incoming", {"CallSid": "CA" + "1" * 32}
    first = TwilioCredentials(SID, SecretStr("1" * 32), integration_id=uuid4())
    second = TwilioCredentials("AC" + "d" * 32, SecretStr("2" * 32), integration_id=uuid4())
    signature = str(
        RequestValidator("2" * 32).compute_signature(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            config.telephony_public_base_url + path, params
        )
    )
    assert matching_credentials(config, [first, second], path, signature, params) is second
    assert matching_credentials(config, [first], path, signature, params) is None
    assert matching_credentials(config, [], path, signature, params) is None
    empty = TwilioCredentials(SID, SecretStr(""))
    assert matching_credentials(config, [empty], path, signature, params) is None
    assert TOKEN not in repr(TwilioCredentials(SID, SecretStr(TOKEN)))
    keyed = TwilioCredentials(SID, SecretStr(TOKEN), "SK" + "e" * 32, SecretStr("key-secret"))
    assert keyed.rest_auth == ("SK" + "e" * 32, "key-secret")
    assert TwilioCredentials(SID, SecretStr(TOKEN)).rest_auth == (SID, TOKEN)


def test_circuit_breakers_are_scoped_per_connection() -> None:
    registry = CircuitRegistry(max_entries=2)
    first = registry.get("stt:tenant-a", 1, 60)
    first.record_failure()
    assert not first.allow_request()
    assert registry.get("stt:tenant-b", 1, 60).allow_request()  # Unaffected neighbour.
    assert registry.get("stt:tenant-a", 1, 60) is first
    registry.get("stt:tenant-c", 1, 60)  # Evicts the least recently used entry.
    assert registry.get("stt:tenant-b", 1, 60).allow_request()
    assert registry.get("stt:tenant-a", 1, 60) is not first


def test_settings_require_keys_only_when_tenant_integrations_are_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "false")
    base = {
        "database_url": SecretStr("postgresql+asyncpg://app:app@localhost:5432/frontdesk"),
        "redis_url": SecretStr("redis://localhost:6379/0"),
    }

    def build(**overrides: object) -> Settings:
        return Settings.from_environment(env_file=None, **base, **overrides)

    assert not build().tenant_integrations_enabled
    with pytest.raises(ValidationError):
        build(tenant_integrations_enabled=True)  # No encryption keyring.
    ring = {"secret_encryption_keys": keyring(), "secret_encryption_active_key": "k1"}
    tenant = uuid4()
    enabled = build(
        tenant_integrations_enabled=True, legacy_provider_tenant_ids=f"{tenant}, {uuid4()}", **ring
    )
    assert tenant in enabled.legacy_tenants and len(enabled.legacy_tenants) == 2
    resolver = TenantIntegrationResolver(None, enabled)  # pyright: ignore[reportArgumentType]
    assert resolver.legacy_allowed(tenant) and not resolver.legacy_allowed(uuid4())
    assert TenantIntegrationResolver(None, build()).legacy_allowed(uuid4())  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError):
        build(tenant_integrations_enabled=True, legacy_provider_tenant_ids="not-a-uuid", **ring)
    origin = {"telephony_enabled": True, "telephony_public_base_url": "https://voice.example.test"}
    # Tenant connections supply the Twilio account, but the stream key becomes mandatory.
    with pytest.raises(ValidationError):
        build(tenant_integrations_enabled=True, **ring, **origin)
    assert build(
        tenant_integrations_enabled=True,
        stream_token_key=SecretStr(secrets.token_urlsafe(48)),
        **ring,
        **origin,
    ).telephony_enabled
    with pytest.raises(ValidationError):
        build(**origin)  # Legacy mode still needs the platform Twilio account.
