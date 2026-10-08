"""Prepaid admission, worker fencing and conservative provider-outcome reconciliation.

Write methods are internal worker operations. No tenant or platform HTTP endpoint
can fabricate a dispatch, its measured units, or a verified provider outcome.
"""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.billing.database import financial_transaction, lock_financial_tenant
from app.billing.models import BillingAccount, PlanVersion
from app.billing.schemas import MAX_MONEY, Meter, PlanDefinition
from app.billing.service import InsufficientFundsError, command_hash, lock_tenant, record_ledger
from app.billing.usage_models import UsageCommand, UsageReservation
from app.billing.usage_schemas import (
    ChangeUsage,
    FencedUsage,
    MeterUsageView,
    ReserveUsage,
    SettleUsage,
    UsageSummary,
    UsageView,
)
from app.configuration.models import PublishedConfiguration
from app.configuration.schemas import OperationalConfiguration
from app.database.rls import set_tenant_scope
from app.database.unit_of_work import enter_tenant_scope
from app.errors import ApplicationError, AuthorizationError, ConflictError, NotFoundError
from app.rbac.service import RevisionConflictError
from app.tenants.models import Tenant

ACTIVE = ("held", "running", "reconciliation")
CHANNELS: dict[Meter, str] = {
    "voice_seconds": "voice",
    "sms_messages": "sms",
    "email_messages": "email",
    "chat_messages": "chat",
}
HOLD_LEASE = timedelta(seconds=60)
WORKER_LEASE = timedelta(seconds=30)


class AdmissionDeniedError(ApplicationError):
    status_code = 409
    code = "usage_admission_denied"
    public_message = (
        "Current channel, subscription, budget or usage limits do not permit this work."
    )


class UsageLeaseError(ApplicationError):
    status_code = 409
    code = "usage_lease_expired"
    public_message = "This dispatch lease is no longer valid. Reconcile the provider outcome."


def view(row: UsageReservation) -> UsageView:
    return UsageView.model_validate({name: getattr(row, name) for name in UsageView.model_fields})


def budget_window(now: datetime, timezone: str) -> tuple[datetime, datetime]:
    local = now.astimezone(ZoneInfo(timezone))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start.astimezone(UTC), end.astimezone(UTC)


async def publication(session: AsyncSession, tenant_id: UUID) -> PublishedConfiguration | None:
    return await session.scalar(
        select(PublishedConfiguration)
        .where(PublishedConfiguration.tenant_id == tenant_id)
        .order_by(PublishedConfiguration.version.desc())
        .limit(1)
    )


async def summary(session: AsyncSession, tenant_id: UUID, now: datetime) -> UsageSummary:
    account = await session.scalar(
        select(BillingAccount).where(BillingAccount.tenant_id == tenant_id)
    )
    published = await publication(session, tenant_id)
    configuration = (
        OperationalConfiguration.model_validate(published.document) if published else None
    )
    start, end = (
        budget_window(now, configuration.business.timezone) if configuration else (None, None)
    )
    spent = 0
    if start and end:
        spent = (
            await session.scalar(
                select(
                    func.coalesce(
                        func.sum(UsageReservation.actual_units * UsageReservation.price_minor), 0
                    )
                ).where(
                    UsageReservation.tenant_id == tenant_id,
                    UsageReservation.state == "settled",
                    UsageReservation.settled_at >= start,
                    UsageReservation.settled_at < end,
                )
            )
            or 0
        )
    slots = (
        await session.scalar(
            select(func.count())
            .select_from(UsageReservation)
            .where(
                UsageReservation.tenant_id == tenant_id,
                UsageReservation.meter == "voice_seconds",
                UsageReservation.state.in_(ACTIVE),
            )
        )
        or 0
    )
    reconciliation = (
        await session.scalar(
            select(func.count())
            .select_from(UsageReservation)
            .where(
                UsageReservation.tenant_id == tenant_id,
                UsageReservation.state == "reconciliation",
            )
        )
        or 0
    )
    meters: list[MeterUsageView] = []
    concurrency = 0
    if account and account.plan_version_id:
        plan = await session.get(PlanVersion, account.plan_version_id)
        if plan is None:
            raise RuntimeError("Missing pinned billing plan")
        definition = PlanDefinition.model_validate(plan.definition)
        concurrency = definition.voice_concurrency
        for meter in definition.meters:
            scope = (
                UsageReservation.tenant_id == tenant_id,
                UsageReservation.plan_version_id == plan.id,
                UsageReservation.period_start == account.period_start,
                UsageReservation.meter == meter.meter,
            )
            consumed = (
                await session.scalar(
                    select(func.coalesce(func.sum(UsageReservation.actual_units), 0)).where(
                        *scope,
                        UsageReservation.state == "settled",
                    )
                )
                or 0
            )
            reserved = (
                await session.scalar(
                    select(func.coalesce(func.sum(UsageReservation.max_units), 0)).where(
                        *scope,
                        UsageReservation.state.in_(ACTIVE),
                    )
                )
                or 0
            )
            meters.append(
                MeterUsageView(
                    meter=meter.meter,
                    price_minor=meter.price_minor,
                    monthly_limit=meter.monthly_limit,
                    consumed_units=consumed,
                    reserved_units=reserved,
                )
            )
    return UsageSummary(
        period_start=account.period_start if account else None,
        period_end=account.period_end if account else None,
        budget_timezone=configuration.business.timezone if configuration else None,
        budget_start=start,
        budget_end=end,
        budget_limit_minor=configuration.budget.monthly_budget_paisa if configuration else 0,
        spent_minor=spent,
        reserved_minor=account.reserved_minor if account else 0,
        voice_concurrency_limit=concurrency,
        voice_slots_reserved=slots,
        reconciliation_count=reconciliation,
        meters=meters,
    )


async def save_command(
    session: AsyncSession,
    context: RequestContext,
    operation: str,
    command: BaseModel,
    row: UsageReservation,
) -> UsageView:
    result = view(row)
    session.add(
        UsageCommand(
            id=uuid4(),
            tenant_id=context.tenant_id,
            mutation_id=command.model_dump()["mutation_id"],
            command_hash=command_hash(command),
            request_id=context.request_id,
            result={"operation": operation, "reservation": result.model_dump(mode="json")},
        )
    )
    await session.flush()
    return result


async def usage_account(session: AsyncSession, tenant_id: UUID) -> BillingAccount:
    account = await session.scalar(
        select(BillingAccount).where(BillingAccount.tenant_id == tenant_id)
    )
    if account is None:
        raise AdmissionDeniedError()
    return account


class UsageService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        financial_sessions: async_sessionmaker[AsyncSession] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._financial_sessions = financial_sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get_summary(self, context: RequestContext) -> UsageSummary:
        require_permission(context, Permission.BILLING_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            return await summary(session, context.tenant_id, self._clock())

    @asynccontextmanager
    async def _write(
        self, context: RequestContext
    ) -> AsyncGenerator[tuple[AsyncSession, AsyncSession, Tenant]]:
        if context.actor_type != ActorType.SYSTEM or any(
            (
                context.actor_id,
                context.platform_user_id,
                context.call_id,
                context.human_session_id,
                context.support_session_id,
                context.role,
            )
        ):
            raise AuthorizationError()
        async with self._sessions.begin() as authority:
            await enter_tenant_scope(authority, context)
            tenant = await lock_tenant(authority, context.tenant_id)
            async with financial_transaction(self._financial_sessions) as writer:
                await set_tenant_scope(writer, context.tenant_id)
                await lock_financial_tenant(writer, context.tenant_id)
                yield writer, authority, tenant

    async def _receipt(
        self, session: AsyncSession, context: RequestContext, operation: str, command: BaseModel
    ) -> UsageView | None:
        mutation_id = command.model_dump()["mutation_id"]
        prior = await session.scalar(
            select(UsageCommand).where(
                UsageCommand.tenant_id == context.tenant_id,
                UsageCommand.mutation_id == mutation_id,
            )
        )
        if prior:
            if (
                prior.command_hash != command_hash(command)
                or prior.result.get("operation") != operation
            ):
                raise ConflictError()
            return UsageView.model_validate(prior.result["reservation"])
        return None

    async def _row(
        self, session: AsyncSession, tenant_id: UUID, command: ChangeUsage
    ) -> UsageReservation:
        row = await session.scalar(
            select(UsageReservation).where(
                UsageReservation.tenant_id == tenant_id,
                UsageReservation.id == command.reservation_id,
            )
        )
        if row is None:
            raise NotFoundError()
        if row.revision != command.revision:
            raise RevisionConflictError()
        return row

    async def _live_dispatch(
        self,
        writer: AsyncSession,
        authority: AsyncSession,
        tenant: Tenant,
        identity: UUID,
        fence: UUID,
    ) -> UsageView:
        row = await writer.scalar(
            select(UsageReservation).where(
                UsageReservation.tenant_id == tenant.id, UsageReservation.id == identity
            )
        )
        if row is None:
            raise NotFoundError()
        if row.state != "running" or row.fence != fence or row.lease_expires_at <= self._clock():
            raise UsageLeaseError()
        account = await usage_account(writer, tenant.id)
        await self._allowed(
            authority,
            writer,
            tenant,
            account,
            view(row).meter,
            row.max_units,
            row.quoted_minor,
            adding=False,
        )
        return view(row)

    async def check_dispatch(
        self, context: RequestContext, identity: UUID, fence: UUID
    ) -> UsageView:
        """Recheck before provider dispatch/actions. A saved receipt is not authorization."""
        async with self._write(context) as (writer, authority, tenant):
            return await self._live_dispatch(writer, authority, tenant, identity, fence)

    async def _allowed(
        self,
        authority: AsyncSession,
        writer: AsyncSession,
        tenant: Tenant,
        account: BillingAccount,
        meter: Meter,
        units: int,
        quoted: int,
        *,
        adding: bool,
    ) -> int:
        now = self._clock()
        if (
            not tenant.is_active
            or tenant.service_state != "live"
            or not account.plan_version_id
            or not account.period_start
            or not account.period_end
            or not account.period_start <= now < account.period_end
        ):
            raise AdmissionDeniedError()
        published = await publication(authority, tenant.id)
        if published is None:
            raise AdmissionDeniedError()
        config = OperationalConfiguration.model_validate(published.document)
        enabled = {
            "voice_seconds": config.voice.enabled,
            "sms_messages": config.messaging.sms_enabled,
            "email_messages": config.messaging.email_enabled,
            "chat_messages": config.chat.enabled,
        }
        plan = await writer.get(PlanVersion, account.plan_version_id)
        if plan is None:
            raise AdmissionDeniedError()
        definition = PlanDefinition.model_validate(plan.definition)
        price = next((item for item in definition.meters if item.meter == meter), None)
        if not enabled[meter] or CHANNELS[meter] not in definition.entitlements or price is None:
            raise AdmissionDeniedError()
        usage = await summary(authority, tenant.id, now)
        purchased = next((item for item in usage.meters if item.meter == meter), None)
        # authority and writer use separate pools; the writer has no clinical/configuration access.
        if (
            purchased is None
            or purchased.consumed_units + purchased.reserved_units + (units if adding else 0)
            > price.monthly_limit
        ):
            raise AdmissionDeniedError()
        if (
            usage.spent_minor + usage.reserved_minor + (quoted if adding else 0)
            > config.budget.monthly_budget_paisa
        ):
            raise AdmissionDeniedError()
        if (
            meter == "voice_seconds"
            and usage.voice_slots_reserved + int(adding) > definition.voice_concurrency
        ):
            raise AdmissionDeniedError()
        if adding and quoted > account.balance_minor - account.reserved_minor:
            raise InsufficientFundsError()
        return published.version

    async def reserve(self, context: RequestContext, command: ReserveUsage) -> UsageView:
        async with self._write(context) as (writer, authority, tenant):
            prior = await self._receipt(writer, context, "reserve", command)
            if prior:
                return prior
            exists = await writer.scalar(
                select(UsageReservation.id).where(
                    UsageReservation.tenant_id == tenant.id,
                    (UsageReservation.id == command.reservation_id)
                    | (UsageReservation.resource_key == command.resource_key),
                )
            )
            if exists:
                raise ConflictError()
            account = await usage_account(writer, tenant.id)
            plan = (
                await writer.get(PlanVersion, account.plan_version_id)
                if account.plan_version_id
                else None
            )
            if plan is None or not account.period_start or not account.period_end:
                raise AdmissionDeniedError()
            definition = PlanDefinition.model_validate(plan.definition)
            price = next((item for item in definition.meters if item.meter == command.meter), None)
            if price is None:
                raise AdmissionDeniedError()
            quoted = command.max_units * price.price_minor
            if quoted > MAX_MONEY:
                raise AdmissionDeniedError()
            version = await self._allowed(
                authority,
                writer,
                tenant,
                account,
                command.meter,
                command.max_units,
                quoted,
                adding=True,
            )
            row = UsageReservation(
                id=command.reservation_id,
                tenant_id=tenant.id,
                resource_key=command.resource_key,
                revision=1,
                meter=command.meter,
                state="held",
                plan_version_id=plan.id,
                period_start=account.period_start,
                period_end=account.period_end,
                configuration_version=version,
                max_units=command.max_units,
                price_minor=price.price_minor,
                quoted_minor=quoted,
                actual_units=None,
                lease_expires_at=self._clock() + HOLD_LEASE,
                fence=None,
                settled_at=None,
                outcome_reference=None,
            )
            writer.add(row)
            record_ledger(
                writer,
                account,
                mutation_id=command.mutation_id,
                kind="hold",
                amount=0,
                reserved_delta=quoted,
                reason=f"Usage hold {row.id}",
                request_id=context.request_id,
            )
            return await save_command(writer, context, "reserve", command, row)

    async def start(self, context: RequestContext, command: ChangeUsage) -> UsageView:
        async with self._write(context) as (writer, authority, tenant):
            prior = await self._receipt(writer, context, "start", command)
            if prior:
                if prior.fence is None:
                    raise UsageLeaseError()
                return await self._live_dispatch(writer, authority, tenant, prior.id, prior.fence)
            row = await self._row(writer, tenant.id, command)
            if row.state != "held" or row.lease_expires_at <= self._clock():
                raise UsageLeaseError()
            account = await usage_account(writer, tenant.id)
            await self._allowed(
                authority,
                writer,
                tenant,
                account,
                view(row).meter,
                row.max_units,
                row.quoted_minor,
                adding=False,
            )
            row.state = "running"
            row.fence = uuid4()
            row.lease_expires_at = self._clock() + WORKER_LEASE
            row.revision += 1
            return await save_command(writer, context, "start", command, row)

    async def heartbeat(self, context: RequestContext, command: FencedUsage) -> UsageView:
        async with self._write(context) as (writer, authority, tenant):
            prior = await self._receipt(writer, context, "heartbeat", command)
            if prior:
                return await self._live_dispatch(writer, authority, tenant, prior.id, command.fence)
            row = await self._row(writer, tenant.id, command)
            if (
                row.state != "running"
                or row.fence != command.fence
                or row.lease_expires_at <= self._clock()
            ):
                raise UsageLeaseError()
            account = await usage_account(writer, tenant.id)
            await self._allowed(
                authority,
                writer,
                tenant,
                account,
                view(row).meter,
                row.max_units,
                row.quoted_minor,
                adding=False,
            )
            row.revision += 1
            row.lease_expires_at = self._clock() + WORKER_LEASE
            return await save_command(writer, context, "heartbeat", command, row)

    async def settle(self, context: RequestContext, command: SettleUsage) -> UsageView:
        """Only a trusted provider adapter with a verified terminal outcome calls this."""
        async with self._write(context) as (writer, _, tenant):
            prior = await self._receipt(writer, context, "settle", command)
            if prior:
                return prior
            row = await self._row(writer, tenant.id, command)
            if (
                row.state not in ("running", "reconciliation")
                or row.fence != command.fence
                or command.actual_units > row.max_units
            ):
                raise ConflictError()
            account = await usage_account(writer, tenant.id)
            row.revision += 1
            row.state = "settled"
            row.actual_units = command.actual_units
            row.settled_at = self._clock()
            row.outcome_reference = command.outcome_reference
            record_ledger(
                writer,
                account,
                mutation_id=command.mutation_id,
                kind="usage",
                amount=-command.actual_units * row.price_minor,
                reserved_delta=-row.quoted_minor,
                reason=f"Usage settlement {row.id}",
                request_id=context.request_id,
            )
            return await save_command(writer, context, "settle", command, row)

    async def release(self, context: RequestContext, command: ChangeUsage) -> UsageView:
        """Cancel only work that has never received permission to dispatch."""
        async with self._write(context) as (writer, _, tenant):
            prior = await self._receipt(writer, context, "release", command)
            if prior:
                return prior
            row = await self._row(writer, tenant.id, command)
            if row.state != "held":
                raise ConflictError()
            account = await usage_account(writer, tenant.id)
            row.revision += 1
            row.state = "released"
            row.actual_units = 0
            row.settled_at = self._clock()
            row.outcome_reference = "not-dispatched"
            record_ledger(
                writer,
                account,
                mutation_id=command.mutation_id,
                kind="release",
                amount=0,
                reserved_delta=-row.quoted_minor,
                reason=f"Unused usage hold {row.id}",
                request_id=context.request_id,
            )
            return await save_command(writer, context, "release", command, row)

    async def expire(self, context: RequestContext) -> int:
        """Bounded per-tenant sweep. Unknown dispatched work keeps money and slots."""
        async with self._write(context) as (writer, _, _):
            return await expire_reservations(writer, context, self._clock())


async def expire_reservations(writer: AsyncSession, context: RequestContext, now: datetime) -> int:
    """Caller owns tenant scope and the financial transaction mutex."""
    rows = list(
        await writer.scalars(
            select(UsageReservation)
            .where(
                UsageReservation.tenant_id == context.tenant_id,
                UsageReservation.state.in_(("held", "running")),
                UsageReservation.lease_expires_at <= now,
            )
            .order_by(UsageReservation.lease_expires_at, UsageReservation.id)
            .limit(100)
        )
    )
    for row in rows:
        command = ChangeUsage(
            mutation_id=uuid5(row.id, "lease-expired"),
            reservation_id=row.id,
            revision=row.revision,
        )
        if row.state == "held":
            account = await usage_account(writer, context.tenant_id)
            row.state = "released"
            row.actual_units = 0
            row.settled_at = now
            row.outcome_reference = "unused-lease-expired"
            record_ledger(
                writer,
                account,
                mutation_id=command.mutation_id,
                kind="release",
                amount=0,
                reserved_delta=-row.quoted_minor,
                reason=f"Expired unused hold {row.id}",
                request_id=context.request_id,
            )
        else:
            row.state = "reconciliation"
        row.revision += 1
        await save_command(writer, context, "expire", command, row)
    return len(rows)
