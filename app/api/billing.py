from typing import Annotated, get_args
from uuid import UUID

from fastapi import APIRouter, Query, Request

from app.api.platform import PlatformIdentity
from app.api.tenancy import Context, DatabaseDependency
from app.auth.policies import Permission
from app.billing.database import FinancialDatabase, FinancialUnavailableError
from app.billing.schemas import (
    BillingMutationView,
    BillingView,
    Entitlement,
    FinancialAdjustment,
    LedgerKind,
    LedgerPage,
    Meter,
    PlanDefinition,
    PlanView,
    PublishPlan,
    SelectPlan,
)
from app.billing.service import BillingService, PlatformBillingService
from app.billing.usage import UsageService
from app.billing.usage_schemas import UsageSummary
from app.platform.support import SUPPORT_ROUTES
from app.security.encryption import SecretCipher

router = APIRouter(prefix="/api/v2/billing", tags=["workspace billing"])
platform_router = APIRouter(prefix="/api/v2/platform/billing", tags=["platform billing"])


def service(request: Request, database: DatabaseDependency) -> BillingService:
    writer = getattr(request.app.state, "financial_database", None)
    return BillingService(
        database.session_factory,
        writer.session_factory if isinstance(writer, FinancialDatabase) else None,
    )


def platform_service(request: Request, database: DatabaseDependency) -> PlatformBillingService:
    writer = getattr(request.app.state, "financial_database", None)
    return PlatformBillingService(
        database.session_factory,
        writer.session_factory if isinstance(writer, FinancialDatabase) else None,
    )


@router.get("")
async def overview(request: Request, context: Context, database: DatabaseDependency) -> BillingView:
    return await service(request, database).get(context)


@router.get("/catalog")
async def catalog(
    request: Request, context: Context, database: DatabaseDependency
) -> dict[str, object]:
    return {
        "plans": await service(request, database).catalog(context),
        "currency": "INR",
        "meters": list(get_args(Meter)),
        "entitlements": list(get_args(Entitlement)),
        "subscription_policy": "prepaid_calendar_month",
        "active_plan_change": "at_period_end",
        "auto_renewal_available": False,
        "payments_available": False,
        "runtime_dispatch_ready": False,
        "usage_admission_available": True,
        "budget_policy": "published_timezone_calendar_month_metered_usage",
        "unknown_dispatch_policy": "keep_funds_and_concurrency_until_verified_outcome",
        "ledger_kinds": list(get_args(LedgerKind)),
        "plan_schema": PlanDefinition.model_json_schema(),
    }


@router.get("/usage")
async def usage_summary(context: Context, database: DatabaseDependency) -> UsageSummary:
    return await UsageService(database.session_factory).get_summary(context)


@router.post("/subscription")
async def subscription(
    command: SelectPlan, request: Request, context: Context, database: DatabaseDependency
) -> BillingMutationView:
    return await service(request, database).select_plan(context, command)


@router.get("/mutations/{identity}")
async def mutation(
    identity: UUID, request: Request, context: Context, database: DatabaseDependency
) -> BillingMutationView:
    return await service(request, database).mutation(context, identity)


@router.get("/ledger")
async def ledger(
    request: Request,
    context: Context,
    database: DatabaseDependency,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=25, le=100)] = 25,
    q: Annotated[str | None, Query(max_length=200)] = None,
    kind: LedgerKind | None = None,
) -> LedgerPage:
    settings = request.app.state.settings
    try:
        cipher = SecretCipher(
            settings.secret_encryption_keys, settings.secret_encryption_active_key
        )
    except ValueError:
        raise FinancialUnavailableError() from None
    return await service(request, database).ledger(
        context, cipher, cursor=cursor, limit=limit, q=q, kind=kind
    )


@platform_router.get("/plans")
async def platform_plans(
    request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> list[PlanView]:
    return await platform_service(request, database).catalog(context)


@platform_router.get("/catalog")
async def platform_catalog(
    request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> dict[str, object]:
    return {
        "plans": await platform_service(request, database).catalog(context),
        "schema": PlanDefinition.model_json_schema(),
    }


@platform_router.get("/plans/mutations/{identity}")
async def plan_receipt(
    identity: UUID, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> PlanView:
    return await platform_service(request, database).plan_receipt(context, identity)


@platform_router.get("/tenants/{tenant_id}")
async def tenant_billing(
    tenant_id: UUID, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> BillingView:
    return await platform_service(request, database).tenant_billing(context, tenant_id)


@platform_router.get("/tenants/{tenant_id}/mutations/{identity}")
async def adjustment_receipt(
    tenant_id: UUID,
    identity: UUID,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> BillingMutationView:
    return await platform_service(request, database).adjustment_receipt(
        context, tenant_id, identity
    )


@platform_router.post("/plans")
async def publish_plan(
    command: PublishPlan, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> PlanView:
    return await platform_service(request, database).publish_plan(context, command)


@platform_router.post("/tenants/{tenant_id}/adjustments")
async def adjustment(
    tenant_id: UUID,
    command: FinancialAdjustment,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> BillingMutationView:
    return await platform_service(request, database).adjust(context, tenant_id, command)


for method, path, permission in (
    ("GET", "", Permission.BILLING_READ),
    ("GET", "/catalog", Permission.BILLING_READ),
    ("GET", "/ledger", Permission.BILLING_READ),
    ("GET", "/usage", Permission.BILLING_READ),
    ("GET", "/mutations/{identity}", Permission.BILLING_READ),
    ("POST", "/subscription", Permission.BILLING_MANAGE),
):
    SUPPORT_ROUTES[(method, f"/api/v2/billing{path}")] = permission
