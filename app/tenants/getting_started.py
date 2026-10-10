"""What a new clinic still has to do, worked out from what it has actually done."""

from typing import Literal

from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.models import PhoneNumber
from app.clinic.models import ProviderSchedule, Service
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import NotFoundError
from app.knowledge.models import Document
from app.providers.models import Provider
from app.settings.store import load_settings
from app.tenants.models import Tenant
from app.users.models import User

StepKey = Literal[
    "clinic_details", "provider", "appointment_types", "knowledge", "team", "phone_number", "live"
]


class Step(BaseModel):
    key: StepKey
    done: bool


class GettingStarted(BaseModel):
    steps: list[Step]
    complete: bool


class GettingStartedService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, context: RequestContext) -> GettingStarted:
        require_permission(context, Permission.TENANT_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            tenant_id = context.tenant_id
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                raise NotFoundError()
            clinic = (await load_settings(session, tenant_id)).clinic

            async def has(query: Select[tuple[int]]) -> bool:
                return bool(await session.scalar(query))

            done: dict[StepKey, bool] = {
                "clinic_details": bool(clinic.address and clinic.phone),
                # A provider who works no hours cannot be booked.
                "provider": await has(
                    select(func.count())
                    .select_from(Provider)
                    .join(ProviderSchedule, ProviderSchedule.provider_id == Provider.id)
                    .where(Provider.tenant_id == tenant_id, Provider.is_active.is_(True))
                ),
                "appointment_types": await has(
                    select(func.count())
                    .select_from(Service)
                    .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
                ),
                "knowledge": await has(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.tenant_id == tenant_id, Document.status == "approved")
                ),
                "team": (
                    await session.scalar(
                        select(func.count())
                        .select_from(User)
                        .where(
                            User.tenant_id == tenant_id,
                            User.is_active.is_(True),
                            User.accepted_at.is_not(None),
                        )
                    )
                    or 0
                )
                > 1,
                "phone_number": await has(
                    select(func.count())
                    .select_from(PhoneNumber)
                    .where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.is_active.is_(True))
                ),
                "live": tenant.service_state == "live",
            }
            steps = [Step(key=key, done=value) for key, value in done.items()]
            return GettingStarted(steps=steps, complete=all(step.done for step in steps))
