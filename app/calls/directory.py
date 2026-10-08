from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.calls.access import active_call
from app.calls.models import Call, PhoneNumber
from app.calls.service import CallContext, audit
from app.configuration.models import PublishedConfiguration
from app.configuration.schemas import OperationalConfiguration
from app.database.rls import set_tenant_scope, verify_runtime_role
from app.errors import AuthorizationError
from app.locations.models import Location
from app.providers.models import Provider
from app.tenants.models import Tenant


class DirectoryQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    limit: int = Field(default=5, ge=1, le=20)
    offset: int = Field(default=0, ge=0, le=1000)


class DirectoryResult(BaseModel):
    items: list[dict[str, str | None]]


class CallDirectoryService:
    """Public clinic directory only. No patient or appointment reads for anonymous callers."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def business_read(
        self, context: CallContext, kind: Literal["services", "staff"], query: DirectoryQuery
    ) -> DirectoryResult:
        """Read public facts from this call's pinned publication, never contact credentials."""
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            call = await active_call(session, context)
            publication = await session.scalar(
                select(PublishedConfiguration).where(
                    PublishedConfiguration.tenant_id == context.tenant_id,
                    PublishedConfiguration.version == call.configuration_version,
                )
            )
            if publication is None:
                return DirectoryResult(items=[])
            snapshot = OperationalConfiguration.model_validate(publication.document).directory
            items: list[dict[str, str | None]] = []
            if kind == "services":
                records = sorted(
                    (
                        row
                        for row in snapshot.services
                        if row.is_active
                        and (
                            call.location_id is None or row.location_id in (None, call.location_id)
                        )
                    ),
                    key=lambda row: (row.name, row.id),
                )
                items = [
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "description": row.description[:500],
                        "price_minor": str(row.price_minor),
                        "currency": row.currency,
                        "duration_minutes": str(row.duration_minutes),
                        "location_id": str(row.location_id) if row.location_id else None,
                    }
                    for row in records[query.offset : query.offset + min(query.limit, 10)]
                ]
            else:
                staff = sorted(
                    (
                        row
                        for row in snapshot.staff
                        if row.is_active
                        and (
                            call.location_id is None or row.location_id in (None, call.location_id)
                        )
                    ),
                    key=lambda row: (row.name, row.id),
                )
                items = [
                    {
                        "id": str(row.id),
                        "name": row.name,
                        "job_title": row.job_title,
                        "location_id": str(row.location_id) if row.location_id else None,
                    }
                    for row in staff[query.offset : query.offset + min(query.limit, 10)]
                ]
            audit(session, context, "agent_business_" + kind + "_read")
            return DirectoryResult(items=items)

    async def read(
        self, context: CallContext, kind: Literal["locations", "providers"], query: DirectoryQuery
    ) -> DirectoryResult:
        query = DirectoryQuery.model_validate(query.model_dump(), strict=True)
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            active = await session.scalar(
                select(Call.id)
                .join(Tenant, Tenant.id == Call.tenant_id)
                .join(PhoneNumber, PhoneNumber.id == Call.phone_number_id)
                .where(
                    Call.id == context.call_id,
                    Call.tenant_id == context.tenant_id,
                    Call.provider_call_id == context.provider_call_id,
                    Call.status == "active",
                    Call.ended_at.is_(None),
                    Tenant.is_active.is_(True),
                    PhoneNumber.is_active.is_(True),
                )
                .with_for_update(read=True, of=Call)
            )
            if active is None:
                raise AuthorizationError()
            items: list[dict[str, str | None]] = []
            if kind == "locations":
                rows = await session.scalars(
                    select(Location)
                    .where(Location.tenant_id == context.tenant_id)
                    .order_by(Location.id)
                    .limit(query.limit)
                    .offset(query.offset)
                )
                items = [
                    {"id": str(row.id), "name": row.name, "timezone": row.timezone} for row in rows
                ]
            elif kind == "providers":
                providers = await session.scalars(
                    select(Provider)
                    .where(Provider.tenant_id == context.tenant_id)
                    .order_by(Provider.id)
                    .limit(query.limit)
                    .offset(query.offset)
                )
                items = [
                    {
                        "id": str(row.id),
                        "display_name": row.display_name,
                        "specialty": row.specialty,
                        "location_id": str(row.location_id),
                    }
                    for row in providers
                ]
            else:
                raise AuthorizationError()
            audit(session, context, "agent_directory_" + kind + "_read")
            return DirectoryResult(items=items)

    async def provider_label(self, context: CallContext, provider_id: UUID) -> str:
        # Focused repository read, not model-supplied display text.
        from sqlalchemy import select

        from app.database.rls import set_tenant_scope, verify_runtime_role
        from app.locations.models import Location
        from app.providers.models import Provider

        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            await active_call(session, context)
            row = (
                await session.execute(
                    select(Provider.display_name, Location.timezone)
                    .join(Location, Location.id == Provider.location_id)
                    .where(Provider.id == provider_id, Provider.tenant_id == context.tenant_id)
                )
            ).one()
            return f"{row[0]} (clinic timezone {row[1]}; times include their UTC offset)"
