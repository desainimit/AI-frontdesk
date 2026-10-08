from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt

from app.billing.schemas import Command, Meter

UsageState = Literal["held", "running", "reconciliation", "settled", "released"]


class ReserveUsage(Command):
    mutation_id: UUID
    reservation_id: UUID
    resource_key: UUID
    meter: Meter
    max_units: StrictInt = Field(ge=1, le=100_000_000)


class ChangeUsage(Command):
    mutation_id: UUID
    reservation_id: UUID
    revision: StrictInt = Field(ge=1)


class FencedUsage(ChangeUsage):
    fence: UUID


class SettleUsage(FencedUsage):
    actual_units: StrictInt = Field(ge=0, le=100_000_000)
    outcome_reference: str = Field(min_length=1, max_length=200)


class UsageView(BaseModel):
    id: UUID
    resource_key: UUID
    revision: int
    meter: Meter
    state: UsageState
    plan_version_id: UUID
    period_start: datetime
    period_end: datetime
    configuration_version: int
    max_units: int
    price_minor: int
    quoted_minor: int
    actual_units: int | None
    lease_expires_at: datetime
    fence: UUID | None
    settled_at: datetime | None
    outcome_reference: str | None


class MeterUsageView(BaseModel):
    meter: Meter
    price_minor: int
    monthly_limit: int
    consumed_units: int
    reserved_units: int


class UsageSummary(BaseModel):
    period_start: datetime | None
    period_end: datetime | None
    budget_timezone: str | None
    budget_start: datetime | None
    budget_end: datetime | None
    budget_limit_minor: int
    spent_minor: int
    reserved_minor: int
    voice_concurrency_limit: int
    voice_slots_reserved: int
    reconciliation_count: int
    meters: list[MeterUsageView]
