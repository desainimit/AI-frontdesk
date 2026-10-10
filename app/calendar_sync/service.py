"""Connecting a provider's calendar, and asking it what is busy.

A staff member starts the connection from the provider's settings. The provider (or
someone with access to their calendar) signs in at Google or Microsoft and consents;
only the refresh token comes back, and it is stored encrypted.
"""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.calendar_sync.models import (
    AppointmentCalendarLink,
    CalendarBusyBlock,
    CalendarConnection,
    CalendarOAuthState,
)
from app.calendar_sync.providers import CalendarApi, Kind, OAuthApp, calendar_api
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import ApplicationError, ConflictError, NotFoundError
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.events.providers import ProviderError
from app.providers.models import Provider

STATE_LIFETIME = timedelta(minutes=10)
CALLBACK_PATH = "/api/calendar/callback"
PROBLEMS = {
    "calendar_access_lost": "Access to this calendar was withdrawn. Connect it again.",
    "provider_unavailable": "The calendar could not be reached on the last try.",
    "provider_timeout": "The calendar did not answer in time on the last try.",
    "provider_rate_limited": "The calendar asked us to slow down.",
    "provider_rejected": "The calendar refused the last request.",
}


class CalendarNotAvailableError(ConflictError):
    code = "calendar_not_available"
    public_message = (
        "This kind of calendar cannot be connected yet. Ask your platform administrator "
        "to set it up."
    )


class CalendarSignInFailedError(ApplicationError):
    status_code = 422
    code = "calendar_sign_in_failed"
    public_message = "The calendar sign-in did not complete. Start the connection again."


class ConnectCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Kind


class CompleteCalendar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=20, max_length=200)


class CalendarStart(BaseModel):
    # Where to send the browser so the calendar's owner can give consent.
    url: str


class CalendarStatus(BaseModel):
    provider_id: UUID
    connected: bool
    kind: Kind | None = None
    account_email: str | None = None
    status: Literal["connected", "needs_attention"] | None = None
    problem: str | None = None
    last_synced_at: datetime | None = None
    # Appointments whose calendar event was moved or deleted in the calendar itself.
    conflicts: int = 0


class CalendarOptions(BaseModel):
    """Which kinds of calendar the platform is set up to connect."""

    google: bool
    microsoft: bool
    connections: list[CalendarStatus]


def oauth_app(settings: Settings, kind: str) -> OAuthApp | None:
    """The platform's registered app for a calendar provider, when one is saved."""
    if kind == "google":
        client, secret = settings.google_calendar_client_id, settings.google_calendar_client_secret
    else:
        client, secret = (
            settings.microsoft_calendar_client_id,
            settings.microsoft_calendar_client_secret,
        )
    value = secret.get_secret_value()
    return OAuthApp(client, value) if client and value else None


def token_context(tenant_id: UUID, provider_id: UUID, kind: str) -> str:
    return f"calendar:{tenant_id}:{provider_id}:{kind}"


def _status(
    row: CalendarConnection | None, provider_id: UUID, conflicts: int = 0
) -> CalendarStatus:
    if row is None:
        return CalendarStatus(provider_id=provider_id, connected=False)
    return CalendarStatus(
        provider_id=provider_id,
        connected=True,
        kind=row.kind,  # pyright: ignore[reportArgumentType]
        account_email=row.account_email,
        status=row.status,  # pyright: ignore[reportArgumentType]
        problem=PROBLEMS.get(row.last_error, "The last sync did not finish.")
        if row.last_error
        else None,
        last_synced_at=row.last_synced_at,
        conflicts=conflicts,
    )


class CalendarService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions, self._settings, self._transport = sessions, settings, transport

    def _api(self, kind: str) -> CalendarApi:
        app = oauth_app(self._settings, kind)
        if app is None:
            raise CalendarNotAvailableError()
        return calendar_api(kind, app, self._transport)

    def _redirect_uri(self) -> str:
        origin = self._settings.tenant_auth_public_origin.rstrip("/")
        if not origin:
            raise CalendarNotAvailableError()
        return origin + CALLBACK_PATH

    def _cipher(self) -> SecretCipher:
        return SecretCipher(
            self._settings.secret_encryption_keys, self._settings.secret_encryption_active_key
        )

    @staticmethod
    def _audit(
        session: AsyncSession, context: RequestContext, action: str, provider_id: UUID
    ) -> None:
        session.add(
            AuditEvent(
                tenant_id=context.tenant_id,
                actor_id=context.actor_id,
                actor_type=context.actor_type,
                platform_user_id=context.platform_user_id,
                support_session_id=context.support_session_id,
                action=action,
                resource_type="provider",
                resource_id=provider_id,
                request_id=context.request_id,
            )
        )

    async def options(self, context: RequestContext) -> CalendarOptions:
        """Every provider's calendar status, and which kinds can be connected."""
        require_permission(context, Permission.DIRECTORY_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            rows = (
                await session.scalars(
                    select(CalendarConnection).where(
                        CalendarConnection.tenant_id == context.tenant_id
                    )
                )
            ).all()
            conflicts: dict[UUID, int] = {}
            for connection_id in await session.scalars(
                select(AppointmentCalendarLink.connection_id).where(
                    AppointmentCalendarLink.tenant_id == context.tenant_id,
                    AppointmentCalendarLink.status == "conflict",
                )
            ):
                conflicts[connection_id] = conflicts.get(connection_id, 0) + 1
            return CalendarOptions(
                google=oauth_app(self._settings, "google") is not None,
                microsoft=oauth_app(self._settings, "microsoft") is not None,
                connections=[
                    _status(row, row.provider_id, conflicts.get(row.id, 0)) for row in rows
                ],
            )

    async def start(
        self, context: RequestContext, provider_id: UUID, command: ConnectCalendar
    ) -> CalendarStart:
        require_permission(context, Permission.DIRECTORY_WRITE)
        api = self._api(command.kind)
        redirect_uri = self._redirect_uri()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            if await session.get(Provider, provider_id) is None:
                raise NotFoundError()
            # Starting again replaces any attempt that was left unfinished.
            await session.execute(
                delete(CalendarOAuthState).where(
                    CalendarOAuthState.tenant_id == context.tenant_id,
                    CalendarOAuthState.provider_id == provider_id,
                )
            )
            session.add(
                CalendarOAuthState(
                    tenant_id=context.tenant_id,
                    provider_id=provider_id,
                    kind=command.kind,
                    state_digest=hashlib.sha256(state.encode()).hexdigest(),
                    code_verifier=verifier,
                    expires_at=datetime.now(UTC) + STATE_LIFETIME,
                )
            )
        return CalendarStart(url=api.authorization_url(redirect_uri, state, challenge))

    async def complete(self, context: RequestContext, command: CompleteCalendar) -> CalendarStatus:
        """Finish the sign-in the browser came back from, and store the connection."""
        require_permission(context, Permission.DIRECTORY_WRITE)
        digest = hashlib.sha256(command.state.encode()).hexdigest()
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            pending = await session.scalar(
                select(CalendarOAuthState).where(
                    CalendarOAuthState.tenant_id == context.tenant_id,
                    CalendarOAuthState.state_digest == digest,
                )
            )
            if pending is None or pending.expires_at <= datetime.now(UTC):
                raise CalendarSignInFailedError()
            provider_id, kind, verifier = pending.provider_id, pending.kind, pending.code_verifier
            # Single use, whatever happens next.
            await session.delete(pending)
        api = self._api(kind)
        try:
            tokens = await api.exchange(command.code, verifier, self._redirect_uri())
            if not tokens.refresh_token:
                raise CalendarSignInFailedError()
            email = await api.account_email(tokens.access_token)
        except ProviderError:
            raise CalendarSignInFailedError() from None
        key_id, ciphertext = self._cipher().encrypt(
            tokens.refresh_token.encode(), token_context(context.tenant_id, provider_id, kind)
        )
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            row = await session.scalar(
                select(CalendarConnection)
                .where(
                    CalendarConnection.tenant_id == context.tenant_id,
                    CalendarConnection.provider_id == provider_id,
                )
                .with_for_update()
            )
            if row is None:
                row = CalendarConnection(
                    tenant_id=context.tenant_id,
                    provider_id=provider_id,
                    kind=kind,
                    account_email=email[:254],
                    key_id=key_id,
                    ciphertext=ciphertext,
                )
                session.add(row)
            else:
                # Another calendar for the same provider: what was synced from the old
                # one no longer applies.
                await self._forget(session, context.tenant_id, provider_id, row.id)
                row.kind, row.account_email = kind, email[:254]
                row.key_id, row.ciphertext = key_id, ciphertext
            row.status, row.last_error, row.last_synced_at = "connected", None, None
            self._audit(session, context, "provider_calendar_connected", provider_id)
            await session.flush()
            return _status(row, provider_id)

    @staticmethod
    async def _forget(
        session: AsyncSession, tenant_id: UUID, provider_id: UUID, connection_id: UUID
    ) -> None:
        await session.execute(
            delete(CalendarBusyBlock).where(
                CalendarBusyBlock.tenant_id == tenant_id,
                CalendarBusyBlock.provider_id == provider_id,
            )
        )
        await session.execute(
            delete(AppointmentCalendarLink).where(
                AppointmentCalendarLink.tenant_id == tenant_id,
                AppointmentCalendarLink.connection_id == connection_id,
            )
        )

    async def disconnect(self, context: RequestContext, provider_id: UUID) -> None:
        """Stop syncing. Events already in the calendar stay where they are."""
        require_permission(context, Permission.DIRECTORY_WRITE)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            row = await session.scalar(
                select(CalendarConnection)
                .where(
                    CalendarConnection.tenant_id == context.tenant_id,
                    CalendarConnection.provider_id == provider_id,
                )
                .with_for_update()
            )
            if row is None:
                raise NotFoundError()
            await self._forget(session, context.tenant_id, provider_id, row.id)
            await session.delete(row)
            self._audit(session, context, "provider_calendar_disconnected", provider_id)

    async def busy_now(
        self, tenant_id: UUID, provider_id: UUID, starts_at: datetime, ends_at: datetime
    ) -> bool | None:
        """Ask the calendar itself whether this time is taken, just before a booking.

        None when there is no calendar or it could not be asked: the booking then relies
        on the last sync, and is never refused because a provider was unreachable.
        """
        async with self._sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            row = await session.scalar(
                select(CalendarConnection).where(
                    CalendarConnection.tenant_id == tenant_id,
                    CalendarConnection.provider_id == provider_id,
                    CalendarConnection.status == "connected",
                )
            )
            if row is None:
                return None
            kind, key_id, ciphertext, connection_id = row.kind, row.key_id, row.ciphertext, row.id
            ours = set(
                await session.scalars(
                    select(AppointmentCalendarLink.external_event_id).where(
                        AppointmentCalendarLink.tenant_id == tenant_id,
                        AppointmentCalendarLink.connection_id == connection_id,
                    )
                )
            )
        app = oauth_app(self._settings, kind)
        if app is None:
            return None
        try:
            refresh_token = (
                self._cipher()
                .decrypt(key_id, ciphertext, token_context(tenant_id, provider_id, kind))
                .decode()
            )
            api = calendar_api(kind, app, self._transport)
            events = await api.events(await api.refresh(refresh_token), starts_at, ends_at)
        except (ProviderError, ValueError, InvalidTag):
            return None
        return any(
            event.busy
            and event.id not in ours
            and event.starts_at < ends_at
            and event.ends_at > starts_at
            for event in events
        )
