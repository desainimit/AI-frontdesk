"""Google Calendar and Outlook (Microsoft Graph) over plain HTTPS.

Every call goes to a fixed provider host with a bounded timeout. Provider response
bodies and error text never leave this module; callers get typed errors.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal, cast
from urllib.parse import quote, urlencode

import httpx

from app.events.providers import (
    PermanentProviderError,
    RateLimitedProviderError,
    TransientProviderError,
)

Kind = Literal["google", "microsoft"]
TIMEOUT_SECONDS = 8
# One sync reads at most this many events; a busier calendar is read again next time.
MAX_EVENTS = 1000


class CalendarAccessLostError(PermanentProviderError):
    """The person withdrew consent or the refresh token stopped working."""

    code = "calendar_access_lost"


@dataclass(frozen=True)
class OAuthApp:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str | None


@dataclass(frozen=True)
class ExternalEvent:
    id: str
    starts_at: datetime
    ends_at: datetime
    # False for events marked free/transparent: they do not block bookings.
    busy: bool


@dataclass(frozen=True)
class EventBody:
    title: str
    description: str
    starts_at: datetime
    ends_at: datetime
    # The same for every retry of one write, so the calendar never gets a duplicate.
    key: str


def _raise(response: httpx.Response) -> None:
    if response.status_code < 300:
        return
    if response.status_code in {401, 403}:
        raise CalendarAccessLostError()
    if response.status_code == 429:
        raise RateLimitedProviderError()
    if response.status_code >= 500:
        raise TransientProviderError()
    raise PermanentProviderError()


def _utc(value: str, zone_is_utc: bool = False) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text[:26] if zone_is_utc and "+" not in text else text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class CalendarApi:
    """What the product needs from a calendar provider. Subclasses name the endpoints."""

    kind: Kind
    authorize_url: str
    token_url: str
    scope: str
    authorize_extra: ClassVar[dict[str, str]] = {}

    def __init__(self, app: OAuthApp, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._app, self._transport = app, transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport
        )

    def authorization_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        """Where the person is sent to give consent."""
        query = {
            "client_id": self._app.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **self.authorize_extra,
        }
        return f"{self.authorize_url}?{urlencode(query)}"

    async def _token(self, form: dict[str, str]) -> Tokens:
        try:
            async with self._client() as client:
                response = await client.post(
                    self.token_url,
                    data={
                        "client_id": self._app.client_id,
                        "client_secret": self._app.client_secret,
                        **form,
                    },
                )
        except httpx.HTTPError:
            raise TransientProviderError() from None
        if response.status_code == 400:
            # invalid_grant: the code or the refresh token is no longer good.
            raise CalendarAccessLostError()
        _raise(response)
        body = cast(dict[str, Any], response.json())
        return Tokens(str(body["access_token"]), body.get("refresh_token"))

    async def exchange(self, code: str, verifier: str, redirect_uri: str) -> Tokens:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> str:
        tokens = await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
        return tokens.access_token

    async def _request(
        self, method: str, url: str, token: str, *, allow: tuple[int, ...] = (), **kwargs: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        try:
            async with self._client() as client:
                response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError:
            raise TransientProviderError() from None
        if response.status_code not in allow:
            _raise(response)
        return response

    async def account_email(self, token: str) -> str:
        raise NotImplementedError

    async def events(self, token: str, start: datetime, end: datetime) -> list[ExternalEvent]:
        raise NotImplementedError

    async def create(self, token: str, event: EventBody) -> str:
        raise NotImplementedError

    async def update(self, token: str, event_id: str, event: EventBody) -> bool:
        """False when the event no longer exists in the calendar."""
        raise NotImplementedError

    async def delete(self, token: str, event_id: str) -> None:
        raise NotImplementedError


class GoogleCalendar(CalendarApi):
    kind: Kind = "google"
    authorize_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scope = "openid email https://www.googleapis.com/auth/calendar.events"
    # Without these Google gives no refresh token on a repeat consent.
    authorize_extra: ClassVar[dict[str, str]] = {"access_type": "offline", "prompt": "consent"}
    _events = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

    @staticmethod
    def _body(event: EventBody) -> dict[str, Any]:
        return {
            "summary": event.title,
            "description": event.description,
            "start": {"dateTime": event.starts_at.astimezone(UTC).isoformat()},
            "end": {"dateTime": event.ends_at.astimezone(UTC).isoformat()},
        }

    async def account_email(self, token: str) -> str:
        response = await self._request(
            "GET", "https://openidconnect.googleapis.com/v1/userinfo", token
        )
        return str(cast(dict[str, Any], response.json()).get("email", ""))

    async def events(self, token: str, start: datetime, end: datetime) -> list[ExternalEvent]:
        found: list[ExternalEvent] = []
        page: str | None = None
        while len(found) < MAX_EVENTS:
            params = {
                "timeMin": start.astimezone(UTC).isoformat(),
                "timeMax": end.astimezone(UTC).isoformat(),
                "singleEvents": "true",
                "maxResults": "250",
                "fields": "items(id,status,transparency,start,end),nextPageToken",
            }
            if page:
                params["pageToken"] = page
            body = cast(
                dict[str, Any],
                (await self._request("GET", self._events, token, params=params)).json(),
            )
            for item in cast(list[dict[str, Any]], body.get("items") or []):
                starts = cast(dict[str, Any], item.get("start") or {})
                ends = cast(dict[str, Any], item.get("end") or {})
                # All-day events carry a date, not a time; they do not block slots.
                if item.get("status") == "cancelled" or "dateTime" not in starts:
                    continue
                found.append(
                    ExternalEvent(
                        str(item["id"]),
                        _utc(str(starts["dateTime"])),
                        _utc(str(ends["dateTime"])),
                        item.get("transparency") != "transparent",
                    )
                )
            page = body.get("nextPageToken")
            if not page:
                break
        return found

    async def create(self, token: str, event: EventBody) -> str:
        response = await self._request(
            "POST",
            self._events,
            token,
            json={**self._body(event), "id": event.key},
            allow=(409,),
        )
        # 409: an earlier try already created it under this id.
        if response.status_code == 409:
            return event.key
        return str(cast(dict[str, Any], response.json())["id"])

    async def update(self, token: str, event_id: str, event: EventBody) -> bool:
        response = await self._request(
            "PATCH",
            f"{self._events}/{quote(event_id, safe='')}",
            token,
            json=self._body(event),
            allow=(404, 410),
        )
        return response.status_code < 300

    async def delete(self, token: str, event_id: str) -> None:
        await self._request(
            "DELETE", f"{self._events}/{quote(event_id, safe='')}", token, allow=(404, 410)
        )


class MicrosoftCalendar(CalendarApi):
    kind: Kind = "microsoft"
    authorize_url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    scope = "offline_access User.Read Calendars.ReadWrite"
    _graph = "https://graph.microsoft.com/v1.0/me"
    _utc_times: ClassVar[dict[str, str]] = {"Prefer": 'outlook.timezone="UTC"'}

    @staticmethod
    def _body(event: EventBody) -> dict[str, Any]:
        def moment(value: datetime) -> dict[str, str]:
            return {
                "dateTime": value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC",
            }

        return {
            "subject": event.title,
            "body": {"contentType": "text", "content": event.description},
            "start": moment(event.starts_at),
            "end": moment(event.ends_at),
        }

    async def account_email(self, token: str) -> str:
        body = cast(dict[str, Any], (await self._request("GET", self._graph, token)).json())
        return str(body.get("mail") or body.get("userPrincipalName") or "")

    async def events(self, token: str, start: datetime, end: datetime) -> list[ExternalEvent]:
        found: list[ExternalEvent] = []
        url: str | None = f"{self._graph}/calendarView"
        params: dict[str, str] | None = {
            "startDateTime": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "endDateTime": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
            "$select": "id,start,end,showAs,isCancelled,isAllDay",
            "$top": "100",
        }
        while url and len(found) < MAX_EVENTS:
            response = await self._request(
                "GET", url, token, params=params, headers=dict(self._utc_times)
            )
            body = cast(dict[str, Any], response.json())
            for item in cast(list[dict[str, Any]], body.get("value") or []):
                if item.get("isCancelled") or item.get("isAllDay"):
                    continue
                found.append(
                    ExternalEvent(
                        str(item["id"]),
                        _utc(str(item["start"]["dateTime"]), zone_is_utc=True),
                        _utc(str(item["end"]["dateTime"]), zone_is_utc=True),
                        item.get("showAs") not in {"free", "workingElsewhere"},
                    )
                )
            next_link = body.get("@odata.nextLink")
            # The next link is followed only on the Graph host it was issued by.
            url = (
                next_link
                if isinstance(next_link, str) and next_link.startswith(self._graph)
                else None
            )
            params = None
        return found

    async def create(self, token: str, event: EventBody) -> str:
        response = await self._request(
            "POST",
            f"{self._graph}/events",
            token,
            json={**self._body(event), "transactionId": event.key},
        )
        return str(cast(dict[str, Any], response.json())["id"])

    async def update(self, token: str, event_id: str, event: EventBody) -> bool:
        response = await self._request(
            "PATCH",
            f"{self._graph}/events/{quote(event_id, safe='')}",
            token,
            json=self._body(event),
            allow=(404,),
        )
        return response.status_code < 300

    async def delete(self, token: str, event_id: str) -> None:
        await self._request(
            "DELETE", f"{self._graph}/events/{quote(event_id, safe='')}", token, allow=(404,)
        )


def calendar_api(
    kind: str, app: OAuthApp, transport: httpx.AsyncBaseTransport | None = None
) -> CalendarApi:
    if kind == "google":
        return GoogleCalendar(app, transport)
    if kind == "microsoft":
        return MicrosoftCalendar(app, transport)
    raise ValueError("Unsupported calendar")
