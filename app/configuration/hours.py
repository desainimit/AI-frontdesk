"""Relational draft hours; published snapshots preserve conversation behavior."""

from datetime import date, time
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    String,
    Time,
    UniqueConstraint,
    delete,
    select,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.configuration.schemas import HoursSettings, OperationalConfiguration
from app.database.domain import TenantRecord
from app.locations.models import Location


class HoursPolicy(TenantRecord):
    __tablename__ = "business_hours_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_business_hours_policies_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "location_id",
            name="uq_business_hours_policies_location",
            postgresql_nulls_not_distinct=True,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "location_id"],
            ["locations.tenant_id", "locations.id"],
            name="fk_business_hours_policies_location",
        ),
        CheckConstraint("after_hours IN ('answer_only','callback','transfer')", name="after_hours"),
    )
    location_id: Mapped[UUID | None]
    after_hours: Mapped[str] = mapped_column(String(20))


class BusinessOpeningWindow(TenantRecord):
    __tablename__ = "business_opening_windows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "policy_id"],
            ["business_hours_policies.tenant_id", "business_hours_policies.id"],
            name="fk_business_opening_windows_policy",
        ),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday"),
        CheckConstraint("opens < closes", name="ordered_window"),
    )
    policy_id: Mapped[UUID]
    weekday: Mapped[int]
    opens: Mapped[time] = mapped_column(Time(timezone=False))
    closes: Mapped[time] = mapped_column(Time(timezone=False))


class BusinessHoliday(TenantRecord):
    __tablename__ = "business_holidays"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "policy_id"],
            ["business_hours_policies.tenant_id", "business_hours_policies.id"],
            name="fk_business_holidays_policy",
        ),
        UniqueConstraint("tenant_id", "policy_id", "day", name="uq_business_holidays_day"),
    )
    policy_id: Mapped[UUID]
    day: Mapped[date] = mapped_column(Date)


async def sync_hours(
    session: AsyncSession, tenant_id: UUID, document: OperationalConfiguration
) -> None:
    """Run under the tenant configuration lock, in the same draft/rollback transaction."""
    existing = (
        await session.scalars(select(HoursPolicy).where(HoursPolicy.tenant_id == tenant_id))
    ).all()
    by_location = {row.location_id: row for row in existing}
    wanted: dict[UUID | None, HoursSettings] = {None: document.hours}
    for identity, override in document.scopes.locations.items():
        if override.hours is not None and await session.get(Location, identity) is not None:
            wanted[identity] = override.hours
    for policy in existing:
        await session.execute(
            delete(BusinessOpeningWindow).where(BusinessOpeningWindow.policy_id == policy.id)
        )
        await session.execute(delete(BusinessHoliday).where(BusinessHoliday.policy_id == policy.id))
        if policy.location_id not in wanted:
            await session.delete(policy)
    for location_id, hours in wanted.items():
        policy = by_location.get(location_id)
        if policy is None:
            policy = HoursPolicy(
                tenant_id=tenant_id, location_id=location_id, after_hours=hours.after_hours
            )
            session.add(policy)
            await session.flush()
        policy.after_hours = hours.after_hours
        for window in hours.windows:
            session.add(
                BusinessOpeningWindow(
                    tenant_id=tenant_id, policy_id=policy.id, **window.model_dump()
                )
            )
        for day in hours.holidays:
            session.add(BusinessHoliday(tenant_id=tenant_id, policy_id=policy.id, day=day))
