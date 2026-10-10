"""The worker's side of calendar sync.

Out: every booking, move and cancellation is written to the provider's calendar.
In: the provider's own events are read every few minutes; the time they take is
removed from bookable slots, and an appointment whose calendar event was moved or
deleted there is flagged for a person to look at. Nothing is changed silently.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidTag
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.calendar_sync.models import AppointmentCalendarLink, CalendarBusyBlock, CalendarConnection
from app.calendar_sync.providers import (
    CalendarAccessLostError,
    CalendarApi,
    EventBody,
    ExternalEvent,
    calendar_api,
)
from app.calendar_sync.service import oauth_app, token_context
from app.core.database.rls import set_tenant_scope
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.events.outbox import EventType
from app.events.providers import Notification, ProviderError
from app.events.store import verify_worker_role

logger = logging.getLogger(__name__)

SYNC_EVERY = timedelta(minutes=5)
LOOK_AHEAD = timedelta(days=60)
MAX_BLOCKS = 2000


def merged(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Overlapping or touching busy times as one stretch each, in order."""
    result: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


class CalendarSync:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions, self._settings, self._transport = sessions, settings, transport

    def _api(self, kind: str) -> CalendarApi | None:
        app = oauth_app(self._settings, kind)
        return calendar_api(kind, app, self._transport) if app else None

    def _refresh_token(self, row: CalendarConnection) -> str:
        cipher = SecretCipher(
            self._settings.secret_encryption_keys, self._settings.secret_encryption_active_key
        )
        return cipher.decrypt(
            row.key_id, row.ciphertext, token_context(row.tenant_id, row.provider_id, row.kind)
        ).decode()

    async def _lost(self, tenant_id: UUID, connection_id: UUID, code: str, *, stop: bool) -> None:
        async with self._sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            row = await session.get(CalendarConnection, connection_id)
            if row is not None:
                row.last_error = code
                if stop:
                    row.status = "needs_attention"

    # ---- out: appointment changes into the calendar ------------------------------------

    async def send(self, notification: Notification, *, idempotency_key: str) -> None:
        """Apply one appointment change. Called for every appointment event in the outbox."""
        appointment = notification.appointment
        async with self._sessions.begin() as session:
            await verify_worker_role(session)
            await set_tenant_scope(session, notification.tenant_id)
            connection = await session.scalar(
                select(CalendarConnection).where(
                    CalendarConnection.tenant_id == notification.tenant_id,
                    CalendarConnection.provider_id == appointment.provider_id,
                    CalendarConnection.status == "connected",
                )
            )
            if connection is None:
                return
            link = await session.scalar(
                select(AppointmentCalendarLink).where(
                    AppointmentCalendarLink.tenant_id == notification.tenant_id,
                    AppointmentCalendarLink.appointment_id == appointment.id,
                )
            )
            connection_id, kind = connection.id, connection.kind
            existing = link.external_event_id if link else None
            try:
                refresh_token = self._refresh_token(connection)
            except (ValueError, InvalidTag):
                refresh_token = None
        api = self._api(kind)
        if api is None or refresh_token is None:
            return
        title = appointment.service_name or "Appointment"
        if appointment.patient_name:
            title = f"{appointment.patient_name} · {title}"
        body = EventBody(
            title=title,
            description="Booked through the clinic's front desk.",
            starts_at=appointment.starts_at,
            ends_at=appointment.ends_at,
            # A retried write is recognised by the calendar as the same event.
            key=f"fd{appointment.id.hex}{appointment.version:04x}",
        )
        cancelled = (
            notification.event_type == EventType.APPOINTMENT_CANCELLED
            or appointment.status == "cancelled"
        )
        try:
            token = await api.refresh(refresh_token)
            if cancelled:
                if existing:
                    await api.delete(token, existing)
                event_id = None
            elif existing and await api.update(token, existing, body):
                event_id = existing
            else:
                event_id = await api.create(token, body)
        except CalendarAccessLostError as lost:
            # The person must connect again; retrying this change would not help.
            await self._lost(notification.tenant_id, connection_id, lost.code, stop=True)
            return
        async with self._sessions.begin() as session:
            await set_tenant_scope(session, notification.tenant_id)
            link = await session.scalar(
                select(AppointmentCalendarLink)
                .where(
                    AppointmentCalendarLink.tenant_id == notification.tenant_id,
                    AppointmentCalendarLink.appointment_id == appointment.id,
                )
                .with_for_update()
            )
            if event_id is None:
                if link is not None:
                    await session.delete(link)
                return
            if link is None:
                link = AppointmentCalendarLink(
                    tenant_id=notification.tenant_id,
                    appointment_id=appointment.id,
                    connection_id=connection_id,
                    external_event_id=event_id,
                    starts_at=appointment.starts_at,
                    ends_at=appointment.ends_at,
                )
                session.add(link)
            link.external_event_id, link.connection_id, link.status = (
                event_id,
                connection_id,
                "synced",
            )
            link.starts_at, link.ends_at = appointment.starts_at, appointment.ends_at

    # ---- in: the provider's own events -------------------------------------------------

    async def pull_one(self) -> bool:
        """Sync one calendar that is due. False when none is."""
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await verify_worker_role(session)
            row = await session.scalar(
                select(CalendarConnection)
                .where(
                    CalendarConnection.status == "connected",
                    or_(
                        CalendarConnection.last_synced_at.is_(None),
                        CalendarConnection.last_synced_at < now - SYNC_EVERY,
                    ),
                )
                .order_by(CalendarConnection.last_synced_at.asc().nulls_first())
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            # Claimed: another worker will not pick it up for the next few minutes.
            row.last_synced_at = now
            tenant_id, provider_id, connection_id, kind = (
                row.tenant_id,
                row.provider_id,
                row.id,
                row.kind,
            )
            try:
                refresh_token = self._refresh_token(row)
            except (ValueError, InvalidTag):
                row.status, row.last_error = "needs_attention", "calendar_access_lost"
                return True
        api = self._api(kind)
        if api is None:
            return True
        try:
            token = await api.refresh(refresh_token)
            events = await api.events(token, now - timedelta(hours=1), now + LOOK_AHEAD)
        except CalendarAccessLostError as lost:
            await self._lost(tenant_id, connection_id, lost.code, stop=True)
            return True
        except ProviderError as failure:
            await self._lost(tenant_id, connection_id, failure.code, stop=False)
            logger.warning("calendar_sync_failed", extra={"error_code": failure.code})
            return True
        await self._store(tenant_id, provider_id, connection_id, events, now)
        return True

    async def _store(
        self,
        tenant_id: UUID,
        provider_id: UUID,
        connection_id: UUID,
        events: list[ExternalEvent],
        now: datetime,
    ) -> None:
        by_id = {event.id: event for event in events}
        async with self._sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            links = (
                await session.scalars(
                    select(AppointmentCalendarLink).where(
                        AppointmentCalendarLink.tenant_id == tenant_id,
                        AppointmentCalendarLink.connection_id == connection_id,
                    )
                )
            ).all()
            ours = {link.external_event_id for link in links}
            for link in links:
                if link.ends_at <= now or link.starts_at >= now + LOOK_AHEAD:
                    continue  # Outside what was read; nothing can be said about it.
                event = by_id.get(link.external_event_id)
                moved = event is None or (event.starts_at, event.ends_at) != (
                    link.starts_at,
                    link.ends_at,
                )
                # Flagged for a person; the appointment itself is left as it is.
                link.status = "conflict" if moved else "synced"
            await session.execute(
                delete(CalendarBusyBlock).where(
                    CalendarBusyBlock.tenant_id == tenant_id,
                    CalendarBusyBlock.provider_id == provider_id,
                )
            )
            busy = merged(
                [
                    (event.starts_at, event.ends_at)
                    for event in events
                    if event.busy and event.id not in ours and event.ends_at > event.starts_at
                ]
            )
            session.add_all(
                [
                    CalendarBusyBlock(
                        tenant_id=tenant_id, provider_id=provider_id, starts_at=start, ends_at=end
                    )
                    for start, end in busy[:MAX_BLOCKS]
                ]
            )
            connection = await session.get(CalendarConnection, connection_id)
            if connection is not None:
                connection.last_error = None
