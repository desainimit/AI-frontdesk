import base64
from uuid import uuid4
from xml.etree.ElementTree import fromstring

import pytest
from pydantic import SecretStr, ValidationError
from twilio.request_validator import RequestValidator  # pyright: ignore[reportMissingTypeStubs]

from app.calls.service import CallContext
from app.core.settings import Settings
from app.voice.twilio import decode_audio, parse_message, signature_valid, stream_token, twiml


def voice_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "twilio_account_sid": "AC" + "1" * 32,
            "twilio_auth_token": SecretStr("test-only-signing-secret"),
            "telephony_public_base_url": "https://voice.example.test",
        }
    )


def sign(settings: Settings, path: str, params: dict[str, str] | None = None) -> str:
    return RequestValidator(settings.twilio_auth_token.get_secret_value()).compute_signature(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        settings.telephony_public_base_url + path, params or {}
    )


def test_signatures_and_stream_binding(settings: Settings) -> None:
    config = voice_settings(settings)
    params = {"CallSid": "CA" + "2" * 32, "To": "+15551234567"}
    path = "/webhooks/v1/twilio/incoming"
    signature = sign(config, path, params)
    assert signature_valid(config, path, signature, params)
    assert not signature_valid(config, path + "/other", signature, params)
    assert not signature_valid(config, path, signature, {**params, "To": "+15551234568"})
    assert not signature_valid(
        settings.model_copy(update={"telephony_public_base_url": ""}), path, signature, params
    )
    context = CallContext(uuid4(), uuid4(), params["CallSid"], uuid4())
    xml = fromstring(twiml(config, context))
    stream = xml.find("Connect/Stream")
    assert stream is not None
    assert stream.attrib["url"].startswith("wss://voice.example.test/")
    parameter = stream.find("Parameter")
    assert parameter is not None and parameter.attrib["value"] == stream_token(config, context)
    assert stream_token(config, context) != stream_token(
        config, CallContext(uuid4(), context.call_id, context.provider_call_id, context.request_id)
    )


@pytest.mark.parametrize(
    "origin",
    ["http://test", "https://test/path", "https://test?x=1", "https://user:secret@test"],
)
def test_enabled_configuration_requires_https_origin(settings: Settings, origin: str) -> None:
    config = voice_settings(settings)
    with pytest.raises(ValidationError):
        Settings.model_validate({**config.model_dump(), "telephony_public_base_url": origin})


@pytest.mark.parametrize("payload", ["!bad", "", "A" * 5000])
def test_invalid_audio_is_rejected(payload: str) -> None:
    with pytest.raises(ValueError):
        decode_audio(payload)


def test_message_bounds() -> None:
    assert decode_audio(base64.b64encode(bytes(160)).decode()) == bytes(160)
    for raw in ("[]", '{"event":1}', "x" * 16385):
        with pytest.raises(ValueError):
            parse_message(raw)
