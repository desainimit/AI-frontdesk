"""Relational staging under the shared tenant configuration lock."""

from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.business.models import (
    BusinessResource,
    BusinessService,
    BusinessStaff,
    ServiceResource,
    ServiceStaff,
    StaffHoliday,
    StaffWindow,
)
from app.business.schemas import DirectorySnapshot


async def sync_directory(
    session: AsyncSession,
    tenant_id: UUID,
    snapshot: DirectorySnapshot,
    *,
    changed_id: UUID | None = None,
) -> dict[UUID, int]:
    """Keep identities/history on rollback; entity revisions never move backwards."""
    revisions: dict[UUID, int] = {}
    for model, definitions, excluded in (
        (BusinessStaff, snapshot.staff, {"kind", "availability", "holidays"}),
        (BusinessResource, snapshot.resources, {"kind"}),
        (BusinessService, snapshot.services, {"kind", "staff_ids", "resource_ids"}),
    ):
        existing = {
            row.id: row
            for row in await session.scalars(select(model).where(model.tenant_id == tenant_id))
        }
        wanted = {definition.id for definition in definitions}
        names = {definition.id: definition.name for definition in definitions}
        archived_names: dict[UUID, str] = {}
        # Release names before restoring a valid earlier assignment (including swapped names).
        # Temporary values remain inside this tenant-locked transaction.
        for identity, row in existing.items():
            if identity in names and row.name != names[identity]:
                row.name = f"pending-{uuid4()}"
            elif identity not in wanted and row.name in names.values():
                archived_names[identity] = f"Archived {row.name[:145]} ({uuid4()})"
                row.name = f"pending-{uuid4()}"
        await session.flush()
        for identity, row in existing.items():
            if identity in archived_names:
                row.name = archived_names[identity]
                row.revision += 1
            if identity not in wanted and row.is_active:
                row.is_active = False
                row.revision += 1
        for definition in definitions:
            row = existing.get(definition.id)
            values = definition.model_dump(exclude=excluded)
            if row is None:
                row = model(tenant_id=tenant_id, revision=1, **values)
                session.add(row)
            else:
                if changed_id is None or changed_id == definition.id:
                    row.revision += 1
                for key, value in values.items():
                    setattr(row, key, value)
            revisions[definition.id] = row.revision
        await session.flush()
    for model in (ServiceStaff, ServiceResource, StaffWindow, StaffHoliday):
        await session.execute(delete(model).where(model.tenant_id == tenant_id))
    for record in snapshot.staff:
        for window in record.availability:
            session.add(StaffWindow(tenant_id=tenant_id, staff_id=record.id, **window.model_dump()))
        for day in record.holidays:
            session.add(StaffHoliday(tenant_id=tenant_id, staff_id=record.id, day=day))
    for service in snapshot.services:
        session.add_all(
            [
                ServiceStaff(tenant_id=tenant_id, service_id=service.id, staff_id=identity)
                for identity in service.staff_ids
            ]
        )
        session.add_all(
            [
                ServiceResource(tenant_id=tenant_id, service_id=service.id, resource_id=identity)
                for identity in service.resource_ids
            ]
        )
    await session.flush()
    return revisions
