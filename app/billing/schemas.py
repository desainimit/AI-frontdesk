from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

MAX_MONEY = 1_000_000_000_000
Meter = Literal["voice_seconds", "sms_messages", "email_messages", "chat_messages"]
Entitlement = Literal["voice", "sms", "email", "chat", "scheduling", "workflows", "crm", "tickets"]
LedgerKind = Literal["subscription", "adjustment", "usage", "hold", "release"]


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MeterPrice(Command):
    meter: Meter
    price_minor: StrictInt = Field(ge=0, le=1_000_000)
    monthly_limit: StrictInt = Field(ge=0, le=100_000_000)


class PlanDefinition(Command):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(max_length=2000)
    currency: Literal["INR"] = "INR"
    subscription_price_minor: StrictInt = Field(ge=0, le=1_000_000_000)
    voice_concurrency: StrictInt = Field(ge=0, le=1000)
    entitlements: list[Entitlement] = Field(max_length=8)
    meters: list[MeterPrice] = Field(max_length=4)

    @model_validator(mode="after")
    def valid_catalog(self) -> "PlanDefinition":
        if len(set(self.entitlements)) != len(self.entitlements):
            raise ValueError("Entitlements must be unique")
        if len({item.meter for item in self.meters}) != len(self.meters):
            raise ValueError("Meter prices must be unique")
        if self.voice_concurrency and "voice" not in self.entitlements:
            raise ValueError("Voice concurrency requires the voice entitlement")
        owners = {
            "voice_seconds": "voice",
            "sms_messages": "sms",
            "email_messages": "email",
            "chat_messages": "chat",
        }
        if any(owners[item.meter] not in self.entitlements for item in self.meters):
            raise ValueError("A meter requires its channel entitlement")
        if any(
            channel not in {owners[item.meter] for item in self.meters}
            for channel in self.entitlements
            if channel in owners.values()
        ):
            raise ValueError("Each channel entitlement requires an explicit meter price and limit")
        return self


class PublishPlan(Command):
    mutation_id: UUID
    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]+$")
    expected_version: StrictInt = Field(ge=0)
    definition: PlanDefinition


class PlanView(BaseModel):
    id: UUID
    key: str
    version: int
    definition: PlanDefinition
    published_at: datetime


class SelectPlan(Command):
    mutation_id: UUID
    revision: StrictInt = Field(ge=0)
    plan_version_id: UUID


class FinancialAdjustment(Command):
    mutation_id: UUID
    revision: StrictInt = Field(ge=0)
    amount_minor: StrictInt = Field(ge=-1_000_000_000, le=1_000_000_000)
    reason: str = Field(min_length=12, max_length=500)

    @model_validator(mode="after")
    def nonzero(self) -> "FinancialAdjustment":
        if self.amount_minor == 0:
            raise ValueError("An adjustment must change the balance")
        return self


class SubscriptionView(BaseModel):
    plan: PlanView
    period_start: datetime
    period_end: datetime
    active: bool


class BillingView(BaseModel):
    tenant_id: UUID
    revision: int
    currency: Literal["INR"] = "INR"
    balance_minor: int
    reserved_minor: int
    available_minor: int
    subscription: SubscriptionView | None
    funding_ready: bool
    readiness_issues: list[str]


class BillingMutationView(BaseModel):
    mutation_id: UUID
    revision: int
    kind: Literal["subscription", "adjustment"]
    ledger_entry_id: UUID | None


class LedgerEntryView(BaseModel):
    id: UUID
    revision: int
    kind: LedgerKind
    amount_minor: int
    reserved_delta_minor: int
    balance_after_minor: int
    reserved_after_minor: int
    reason: str
    actor_id: UUID | None
    platform_user_id: UUID | None
    created_at: datetime


class LedgerPage(BaseModel):
    items: list[LedgerEntryView]
    next_cursor: str | None
