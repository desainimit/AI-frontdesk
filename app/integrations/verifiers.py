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

from app.ai.llm.models import DEFAULTS, JOB_FIELDS, JOBS, Job
from app.integrations.catalog import Purpose

OPENROUTER = "https://openrouter.ai/api/v1"
# What each job needs of a model's endpoints.
_NEEDS: dict[Job, str] = {
    "text": "tools",
    "voice": "tools",
    "backup": "tools",
    "quick": "structured_outputs",
}

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
                    # Every model the connection will use, each looked up once.
                    models = (
                        {_job_model("gemini", config, job) for job in JOBS}
                        if purpose == Purpose.LLM
                        else {str(config["model"])}
                    )
                    for model in sorted(models):
                        response = await client.get(
                            "https://generativelanguage.googleapis.com/v1beta/models/"
                            + quote(model, safe=""),
                            headers={"x-goog-api-key": secrets["api_key"]},
                        )
                        # Google answers 400, not 401, for a key it does not recognise.
                        if response.status_code == 400:
                            return Verification("invalid", "credentials_rejected")
                        checked = _classify(response.status_code, missing="model_unavailable")
                        if checked is not VERIFIED:
                            return checked
                    return VERIFIED
                if provider == "openrouter" and purpose in {Purpose.LLM, Purpose.EMBEDDINGS}:
                    # Key information only: this request generates nothing and costs nothing.
                    response = await client.get(
                        "https://openrouter.ai/api/v1/key",
                        headers={"Authorization": f"Bearer {secrets['api_key']}"},
                    )
                    accepted = _classify(response.status_code)
                    if accepted is not VERIFIED or purpose == Purpose.EMBEDDINGS:
                        return accepted
                    return await _openrouter_models(client, config)
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


async def _openrouter_models(client: httpx.AsyncClient, config: Mapping[str, Any]) -> Verification:
    """Every job's model can be used as the receptionist sends it: it is listed, takes
    what the job needs (tools, or a JSON schema for quick checks), and has an endpoint
    that keeps nothing of the conversation. Public lists only: nothing is generated or
    billed."""
    needs: dict[str, set[str]] = {}
    for job in JOBS:
        needs.setdefault(_job_model("openrouter", config, job), set()).add(_NEEDS[job])
    private = await client.get(f"{OPENROUTER}/endpoints/zdr")
    if private.status_code != 200:
        return Verification("unavailable", "provider_unavailable")
    zero_retention = {str(item.get("model_id")) for item in _items(_data(private.json())) if item}
    for model, wanted in sorted(needs.items()):
        author, _, slug = model.partition("/")
        response = await client.get(
            f"{OPENROUTER}/models/{quote(author, safe='')}/{quote(slug, safe='')}/endpoints"
        )
        checked = _classify(response.status_code, missing="model_unavailable")
        if checked is not VERIFIED:
            return checked
        listing: object = _data(response.json())
        endpoints = _items(
            cast(dict[str, Any], listing).get("endpoints") if isinstance(listing, dict) else None
        )
        supported: set[str] = set()
        for endpoint in endpoints:
            supported |= {str(name) for name in _items(endpoint.get("supported_parameters"))}
        if not endpoints:
            return Verification("invalid", "model_unavailable")
        if not wanted <= supported:
            return Verification("invalid", "model_missing_features")
        if model not in zero_retention:
            return Verification("invalid", "model_not_private")
    return VERIFIED


def _job_model(provider: str, config: Mapping[str, Any], job: Job) -> str:
    """The model a job will use: the one chosen, or the default for a job not chosen."""
    return str(config.get(JOB_FIELDS[job]) or DEFAULTS[provider][job])


def _data(body: object) -> Any:
    return cast(dict[str, Any], body).get("data") if isinstance(body, dict) else body


def _items(value: object) -> list[Any]:
    """A list from a provider's response, or empty for anything else."""
    return cast(list[Any], value) if isinstance(value, list) else []
