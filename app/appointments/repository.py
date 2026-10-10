from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.appointments.models import Appointment
from app.auth.context import ActorType, RequestContext
from app.core.database.domain import ScopedRepository


class AppointmentRepository(ScopedRepository[Appointment]):
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        super().__init__(Appointment, session, context)

    async def get(self, identity: UUID, *, lock: bool = False) -> Appointment | None:
        query = select(Appointment).where(
            Appointment.tenant_id == self._context.tenant_id, Appointment.id == identity
        )
        if self._context.actor_type == ActorType.CALLER:
            query = query.where(Appointment.patient_id == self._context.patient_id)
        return await self._session.scalar(query.with_for_update() if lock else query)

    async def search(
        self,
        limit: int,
        offset: int,
        *,
        status: str | None = None,
        provider_id: UUID | None = None,
        patient_id: UUID | None = None,
        starts_from: datetime | None = None,
        starts_to: datetime | None = None,
        newest_first: bool = False,
    ) -> Sequence[Appointment]:
        query = select(Appointment).where(Appointment.tenant_id == self._context.tenant_id)
        if self._context.actor_type == ActorType.CALLER:
            query = query.where(Appointment.patient_id == self._context.patient_id)
        if status is not None:
            query = query.where(Appointment.status == status)
        if provider_id is not None:
            query = query.where(Appointment.provider_id == provider_id)
        if patient_id is not None:
            query = query.where(Appointment.patient_id == patient_id)
        if starts_from is not None:
            query = query.where(Appointment.starts_at >= starts_from)
        if starts_to is not None:
            query = query.where(Appointment.starts_at <= starts_to)
        order = Appointment.starts_at.desc() if newest_first else Appointment.starts_at
        return (
            await self._session.scalars(
                query.order_by(order, Appointment.id).limit(limit).offset(offset)
            )
        ).all()

    async def between(
        self, provider_id: UUID, start: datetime, end: datetime
    ) -> Sequence[Appointment]:
        return (
            await self._session.scalars(
                select(Appointment)
                .where(
                    Appointment.tenant_id == self._context.tenant_id,
                    Appointment.provider_id == provider_id,
                    Appointment.status == "booked",
                    Appointment.starts_at < end,
                    Appointment.ends_at > start,
                )
                .order_by(Appointment.starts_at)
            )
        ).all()
