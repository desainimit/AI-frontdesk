import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID
from xml.etree.ElementTree import Element, SubElement, tostring

from pydantic import SecretStr
from twilio.request_validator import RequestValidator  # pyright: ignore[reportMissingTypeStubs]

from app.calls.service import CallContext
from app.config.settings import Settings

STREAM_TOKEN_SECONDS = 300


@dataclass(frozen=True)
class TwilioCredentials:
    """One Twilio account's identity and secrets for a single call or webhook.

    `integration_id` is set for a clinic's own connection and absent for the platform
    account.
    """

    account_sid: str
    auth_token: SecretStr = field(repr=False)
    api_key_sid: str | None = None
    api_key_secret: SecretStr | None = field(default=None, repr=False)
    integration_id: UUID | None = None
    version_id: UUID | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "TwilioCredentials":
        return cls(settings.twilio_account_sid, settings.twilio_auth_token)

    @property
    def rest_auth(self) -> tuple[str, str]:
        """REST credentials: a scoped API key when supplied, else the account token."""
        if self.api_key_sid is not None and self.api_key_secret is not None:
            return self.api_key_sid, self.api_key_secret.get_secret_value()
        return self.account_sid, self.auth_token.get_secret_value()


def signature_valid(
    settings: Settings,
    path: str,
    signature: str,
    params: dict[str, str] | None = None,
    *,
    websocket: bool = False,
    credentials: TwilioCredentials | None = None,
) -> bool:
    if not settings.telephony_ready or not signature:
        return False
    token = (credentials or TwilioCredentials.from_settings(settings)).auth_token
    if not token.get_secret_value():
        return False
    validator = RequestValidator(token.get_secret_value())
    url = settings.telephony_public_base_url + path
    # Twilio's websocket upgrade may sign its HTTPS handshake or WSS resource URL.
    urls = [url] if not websocket else [url, url.replace("https://", "wss://", 1)]
    return any(validator.validate(candidate, params or {}, signature) for candidate in urls)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def matching_credentials(
    settings: Settings,
    candidates: Sequence[TwilioCredentials],
    path: str,
    signature: str,
    params: dict[str, str] | None = None,
    *,
    websocket: bool = False,
) -> TwilioCredentials | None:
    """The candidate whose signing secret produced this signature, if any."""
    for candidate in candidates:
        if signature_valid(
            settings, path, signature, params, websocket=websocket, credentials=candidate
        ):
            return candidate
    return None


def _platform_stream_token(settings: Settings, context: CallContext) -> str:
    binding = (
        f"frontdesk-stream-v1:{context.tenant_id}:{context.call_id}:{context.provider_call_id}"
    )
    return hmac.new(
        settings.twilio_auth_token.get_secret_value().encode(), binding.encode(), hashlib.sha256
    ).hexdigest()


def _bound_stream_mac(
    settings: Settings, context: CallContext, credentials: TwilioCredentials, expires: int
) -> str:
    # Application signing key, never a provider secret. Bound to tenant, connection,
    # account, call, audience and expiry.
    if len(settings.stream_token_key.get_secret_value()) < 43:
        # An empty key would sign every stream identically; refuse instead.
        raise ValueError("STREAM_TOKEN_KEY is not configured")
    binding = (
        f"frontdesk-stream-v2:media:{context.tenant_id}:{credentials.integration_id}:"
        f"{credentials.account_sid}:{context.call_id}:{context.provider_call_id}:{expires}"
    )
    return hmac.new(
        settings.stream_token_key.get_secret_value().encode(), binding.encode(), hashlib.sha256
    ).hexdigest()


def stream_token(
    settings: Settings, context: CallContext, credentials: TwilioCredentials | None = None
) -> str:
    if credentials is None or credentials.integration_id is None:
        return _platform_stream_token(settings, context)
    expires = int(time.time()) + STREAM_TOKEN_SECONDS
    return f"v2.{expires}.{_bound_stream_mac(settings, context, credentials, expires)}"


def stream_token_valid(
    settings: Settings,
    context: CallContext,
    token: object,
    credentials: TwilioCredentials | None = None,
) -> bool:
    if not isinstance(token, str) or len(token) > 128:
        return False
    if credentials is None or credentials.integration_id is None:
        return hmac.compare_digest(token, _platform_stream_token(settings, context))
    version, _, rest = token.partition(".")
    expires_text, _, mac = rest.partition(".")
    if version != "v2" or not expires_text.isdigit() or len(expires_text) > 12:
        return False
    expires = int(expires_text)
    try:
        expected = _bound_stream_mac(settings, context, credentials, expires)
    except ValueError:
        return False
    return expires >= int(time.time()) and hmac.compare_digest(mac, expected)


def twiml(
    settings: Settings, context: CallContext, credentials: TwilioCredentials | None = None
) -> str:
    root = Element("Response")
    connect = SubElement(root, "Connect")
    path = f"/webhooks/v1/twilio/{context.tenant_id}/{context.call_id}"
    stream = SubElement(
        connect,
        "Stream",
        {
            "url": settings.telephony_public_base_url.replace("https://", "wss://", 1)
            + path
            + "/media",
            "statusCallback": settings.telephony_public_base_url + path + "/status",
            "statusCallbackMethod": "POST",
        },
    )
    SubElement(
        stream,
        "Parameter",
        {"name": "token", "value": stream_token(settings, context, credentials)},
    )
    SubElement(root, "Hangup")
    return tostring(root, encoding="unicode")


def parse_message(raw: str) -> dict[str, Any]:
    if len(raw) > 16384:
        raise ValueError("Oversized media message")
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Invalid media message")
    value = cast(dict[str, Any], parsed)
    if not isinstance(value.get("event"), str):
        raise ValueError("Invalid media message")
    return value


def decode_audio(payload: str) -> bytes:
    if not 1 <= len(payload) <= 4280:
        raise ValueError("Invalid audio length")
    try:
        audio = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid audio encoding") from None
    if not 1 <= len(audio) <= 3200:
        raise ValueError("Invalid audio length")
    return audio
