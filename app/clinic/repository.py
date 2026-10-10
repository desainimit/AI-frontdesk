from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.slots import ProviderCalendar
from app.auth.context import RequestContext
from app.calendar_sync.models import CalendarBusyBlock
from app.clinic.models import ProviderSchedule, ProviderTimeOff, Service
from app.locations.models import Location
from app.providers.models import Provider
from app.settings.schemas import ClinicConfiguration
from app.settings.store import load_settings


class ScheduleRepository:
    """Appointment types, working hours and days off for the clinic in scope."""

    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session = session
        self._tenant_id = context.tenant_id

    async def settings(self) -> ClinicConfiguration:
        return await load_settings(self._session, self._tenant_id)

    async def service(self, identity: UUID, *, lock: bool = False) -> Service | None:
        query = select(Service).where(Service.tenant_id == self._tenant_id, Service.id == identity)
        return await self._session.scalar(query.with_for_update() if lock else query)

    async def services(
        self, limit: int, offset: int, *, active_only: bool = False
    ) -> Sequence[Service]:
        query = select(Service).where(Service.tenant_id == self._tenant_id)
        if active_only:
            query = query.where(Service.is_active.is_(True))
        return (
            await self._session.scalars(
                query.order_by(Service.name, Service.id).limit(limit).offset(offset)
            )
        ).all()

    async def service_names(self, ids: set[UUID]) -> dict[UUID, str]:
        if not ids:
            return {}
        rows = await self._session.execute(
            select(Service.id, Service.name).where(
                Service.tenant_id == self._tenant_id, Service.id.in_(ids)
            )
        )
        return {row[0]: row[1] for row in rows}

    def add(self, row: Service | ProviderSchedule | ProviderTimeOff) -> None:
        self._session.add(row)

    async def flush(self) -> None:
        await self._session.flush()

    async def windows(self, provider_id: UUID) -> Sequence[ProviderSchedule]:
        return (
            await self._session.scalars(
                select(ProviderSchedule)
                .where(
                    ProviderSchedule.tenant_id == self._tenant_id,
                    ProviderSchedule.provider_id == provider_id,
                )
                .order_by(ProviderSchedule.weekday, ProviderSchedule.starts)
            )
        ).all()

    async def clear_windows(self, provider_id: UUID) -> None:
        await self._session.execute(
            delete(ProviderSchedule).where(
                ProviderSchedule.tenant_id == self._tenant_id,
                ProviderSchedule.provider_id == provider_id,
            )
        )

    async def time_off(self, provider_id: UUID) -> Sequence[ProviderTimeOff]:
        return (
            await self._session.scalars(
                select(ProviderTimeOff)
                .where(
                    ProviderTimeOff.tenant_id == self._tenant_id,
                    ProviderTimeOff.provider_id == provider_id,
                )
                .order_by(ProviderTimeOff.starts_on)
            )
        ).all()

    async def remove_time_off(self, provider_id: UUID, identity: UUID) -> bool:
        row = await self._session.scalar(
            select(ProviderTimeOff).where(
                ProviderTimeOff.tenant_id == self._tenant_id,
                ProviderTimeOff.provider_id == provider_id,
                ProviderTimeOff.id == identity,
            )
        )
        if row is None:
            return False
        await self._session.delete(row)
        return True

    async def calendar_busy(
        self, provider_id: UUID, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Time the provider's own calendar says is taken, from the last sync."""
        rows = await self._session.execute(
            select(CalendarBusyBlock.starts_at, CalendarBusyBlock.ends_at)
            .where(
                CalendarBusyBlock.tenant_id == self._tenant_id,
                CalendarBusyBlock.provider_id == provider_id,
                CalendarBusyBlock.starts_at < end,
                CalendarBusyBlock.ends_at > start,
            )
            .order_by(CalendarBusyBlock.starts_at)
        )
        return [(row[0], row[1]) for row in rows]

    async def timezone(self, provider_id: UUID) -> str | None:
        return await self._session.scalar(
            select(Location.timezone)
            .join(Provider, Provider.location_id == Location.id)
            .where(Provider.tenant_id == self._tenant_id, Provider.id == provider_id)
        )

    async def calendar(
        self, provider_id: UUID, settings: ClinicConfiguration
    ) -> ProviderCalendar | None:
        """When an active provider works, or None when they cannot be booked at all."""
        timezone = await self._session.scalar(
            select(Location.timezone)
            .join(Provider, Provider.location_id == Location.id)
            .where(
                Provider.tenant_id == self._tenant_id,
                Provider.id == provider_id,
                Provider.is_active.is_(True),
            )
        )
        if timezone is None:
            return None
        return ProviderCalendar(
            timezone=timezone,
            windows=[
                (row.weekday, row.starts, row.ends) for row in await self.windows(provider_id)
            ],
            time_off=[(row.starts_on, row.ends_on) for row in await self.time_off(provider_id)],
            holidays=frozenset(settings.hours.holidays),
        )
