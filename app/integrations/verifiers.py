"""Read-only provider checks for a submitted credential.

Each check calls a fixed provider host with a bounded timeout and no redirects. It proves
the credential is accepted now; it does not place a call, synthesize audio or spend budget.
Provider response bodies and exception text are never returned or logged.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

import httpx

from app.integrations.catalog import Purpose

Outcome = Literal["verified", "invalid", "unavailable"]


@dataclass(frozen=True)
class Verification:
    outcome: Outcome
    code: str


@dataclass(frozen=True)
class ProviderNumber:
    sid: str
    number: str


class ProviderUnavailableError(RuntimeError):
    """The provider could not be reached or answered unexpectedly."""


class ProviderVerifier(Protocol):
    async def verify(
        self,
        purpose: str,
        provider: str,
        account_identifier: str | None,
        config: Mapping[str, Any],
        secrets: Mapping[str, str],
    ) -> Verification: ...

    async def twilio_numbers(
        self, account_sid: str, secrets: Mapping[str, str], number: str | None = None
    ) -> list[ProviderNumber]: ...


VERIFIED = Verification("verified", "ok")
_REJECTED = {401, 403}


def _classify(status: int, *, missing: str | None = None) -> Verification:
    if status == 200:
        return VERIFIED
    if status in _REJECTED:
        return Verification("invalid", "credentials_rejected")
    if status == 404 and missing is not None:
        return Verification("invalid", missing)
    return Verification("unavailable", "provider_unavailable")


class HttpProviderVerifier:
    def __init__(
        self, timeout_seconds: float = 8, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._timeout, self._transport = timeout_seconds, transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=False, transport=self._transport
        )

    async def verify(
        self,
        purpose: str,
        provider: str,
        account_identifier: str | None,
        config: Mapping[str, Any],
        secrets: Mapping[str, str],
    ) -> Verification:
        if provider == "language_router":
            if purpose not in {Purpose.STT, Purpose.TTS}:
                return Verification("invalid", "unsupported_provider")
            other = "deepgram" if purpose == Purpose.STT else "elevenlabs"
            if not secrets.get("speech_key") or not secrets.get(f"{other}_api_key"):
                return Verification("invalid", "credentials_rejected")
            azure = await self.verify(
                purpose, "azure_speech", None, config, {"speech_key": secrets["speech_key"]}
            )
            if azure.outcome != "verified":
                return azure
            return await self.verify(
                purpose, other, None, config, {"api_key": secrets[f"{other}_api_key"]}
            )
        try:
            async with self._client() as client:
                if provider == "twilio":
                    return await self._twilio(client, account_identifier or "", secrets)
                if provider == "deepgram":
                    response = await client.get(
                        "https://api.deepgram.com/v1/projects",
                        headers={"Authorization": f"Token {secrets['api_key']}"},
                    )
                    return _classify(response.status_code)
                if provider == "elevenlabs":
                    response = await client.get(
                        f"https://api.elevenlabs.io/v1/voices/{config['voice_id']}",
                        headers={"xi-api-key": secrets["api_key"]},
                    )
                    # ElevenLabs answers 400, not 404, for a voice the account does not have.
                    if response.status_code == 400:
                        return Verification("invalid", "voice_not_found")
                    return _classify(response.status_code, missing="voice_not_found")
                if provider == "azure_speech":
                    host = f"{config['region']}.api.cognitive.microsoft.com"
                    response = await client.post(
                        f"https://{host}/sts/v1.0/issueToken",
                        headers={
                            "Ocp-Apim-Subscription-Key": secrets["speech_key"],
                            "Content-Length": "0",
                        },
                    )
                    return _classify(response.status_code)
                if provider == "gemini" and purpose in {Purpose.LLM, Purpose.EMBEDDINGS}:
                    response = await client.get(
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        + quote(str(config["model"]), safe=""),
                        headers={"x-goog-api-key": secrets["api_key"]},
                    )
                    # Google answers 400, not 401, for a key it does not recognise.
                    if response.status_code == 400:
                        return Verification("invalid", "credentials_rejected")
                    return _classify(response.status_code, missing="model_unavailable")
                if provider == "openrouter" and purpose in {Purpose.LLM, Purpose.EMBEDDINGS}:
                    # Key information only: this request generates nothing and costs nothing.
                    response = await client.get(
                        "https://openrouter.ai/api/v1/key",
                        headers={"Authorization": f"Bearer {secrets['api_key']}"},
                    )
                    accepted = _classify(response.status_code)
                    if accepted is not VERIFIED or purpose == Purpose.EMBEDDINGS:
                        return accepted
                    author, _, slug = str(config["model"]).partition("/")
                    response = await client.get(
                        "https://openrouter.ai/api/v1/models/"
                        f"{quote(author, safe='')}/{quote(slug, safe='')}/endpoints"
                    )
                    return _classify(response.status_code, missing="model_unavailable")
        except (httpx.HTTPError, KeyError, ValueError):
            return Verification("unavailable", "provider_unavailable")
        return Verification("invalid", "unsupported_provider")

    async def _twilio(
        self, client: httpx.AsyncClient, account_sid: str, secrets: Mapping[str, str]
    ) -> Verification:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json"
        response = await client.get(url, auth=httpx.BasicAuth(account_sid, secrets["auth_token"]))
        result = _classify(response.status_code, missing="account_not_found")
        if result.outcome != "verified":
            return result
        body = cast(dict[str, Any], response.json())
        if body.get("sid") != account_sid or body.get("status") != "active":
            return Verification("invalid", "account_not_active")
        if "api_key_sid" in secrets:
            keyed = await client.get(
                url, auth=httpx.BasicAuth(secrets["api_key_sid"], secrets["api_key_secret"])
            )
            if keyed.status_code in _REJECTED:
                return Verification("invalid", "api_key_rejected")
            if keyed.status_code != 200:
                return Verification("unavailable", "provider_unavailable")
        return VERIFIED

    async def twilio_numbers(
        self, account_sid: str, secrets: Mapping[str, str], number: str | None = None
    ) -> list[ProviderNumber]:
        """Numbers owned by the verified account; `number` narrows to one E.164 value."""
        params = {"PageSize": "100"}
        if number is not None:
            params["PhoneNumber"] = number
        user = secrets.get("api_key_sid", account_sid)
        password = secrets.get("api_key_secret", secrets["auth_token"])
        try:
            async with self._client() as client:
                response = await client.get(
                    "https://api.twilio.com/2010-04-01/Accounts/"
                    f"{account_sid}/IncomingPhoneNumbers.json",
                    params=params,
                    auth=httpx.BasicAuth(user, password),
                )
                if response.status_code != 200:
                    raise ProviderUnavailableError
                body = cast(dict[str, Any], response.json())
        except (httpx.HTTPError, ValueError):
            raise ProviderUnavailableError from None
        numbers: list[ProviderNumber] = []
        for item in cast(list[dict[str, Any]], body.get("incoming_phone_numbers") or []):
            sid, value = str(item.get("sid", "")), str(item.get("phone_number", ""))
            # Only this account's own resources; ignore anything malformed.
            if (
                item.get("account_sid") == account_sid
                and re.fullmatch(r"PN[0-9a-fA-F]{32}", sid)
                and re.fullmatch(r"\+[1-9][0-9]{7,14}", value)
            ):
                numbers.append(ProviderNumber(sid, value))
        return numbers
