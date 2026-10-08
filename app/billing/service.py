"""All money writes serialize on the tenant; receipts precede revision checks."""

import base64
import calendar
import hashlib
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.billing.database import financial_transaction, lock_financial_tenant
from app.billing.models import BillingAccount, BillingLedger, BillingMutation, PlanVersion
from app.billing.schemas import (
    MAX_MONEY,
    BillingMutationView,
    BillingView,
    FinancialAdjustment,
    LedgerEntryView,
    LedgerKind,
    LedgerPage,
    PlanDefinition,
    PlanView,
    PublishPlan,
    SelectPlan,
    SubscriptionView,
)
from app.billing.usage_models import UsageReservation
from app.database.rls import set_tenant_scope
from app.database.search import contains
from app.database.unit_of_work import enter_tenant_scope
from app.errors import ApplicationError, ConflictError, NotFoundError
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.tenants import platform_scope
from app.rbac.service import RevisionConflictError, audit
from app.security.encryption import SecretCipher
from app.tenants.models import Tenant


class InsufficientFundsError(ApplicationError):
    status_code = 409
    code = "insufficient_funds"
    public_message = "The available balance cannot fund this operation."


class SubscriptionLockedError(ApplicationError):
    status_code = 409
    code = "subscription_locked"
    public_message = (
        "An active subscription cannot be replaced or charged again before its period ends."
    )


class InvalidBillingCursorError(ApplicationError):
    status_code = 422
    code = "invalid_billing_cursor"
    public_message = "This ledger cursor is invalid or expired. Restart from the first page."


def command_hash(command: BaseModel) -> str:
    return hashlib.sha256(
        json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def plan_view(row: PlanVersion) -> PlanView:
    return PlanView(
        id=row.id,
        key=row.key,
        version=row.version,
        definition=PlanDefinition.model_validate(row.definition),
        published_at=row.created_at,
    )


def next_month(value: datetime) -> datetime:
    year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
    return value.replace(
        year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1])
    )


async def billing_view(session: AsyncSession, tenant_id: UUID) -> BillingView:
    account = await session.scalar(
        select(BillingAccount).where(BillingAccount.tenant_id == tenant_id)
    )
    issues: list[str] = []
    subscription: SubscriptionView | None = None
    if account and account.plan_version_id:
        plan = await session.get(PlanVersion, account.plan_version_id)
        if plan is None or account.period_start is None or account.period_end is None:
            raise RuntimeError("Billing subscription invariant failed")
        subscription = SubscriptionView(
            plan=plan_view(plan),
            period_start=account.period_start,
            period_end=account.period_end,
            active=account.period_start <= datetime.now(UTC) < account.period_end,
        )
    if not subscription or not subscription.active:
        issues.append("Select and fund a published subscription for the current period.")
    balance = account.balance_minor if account else 0
    reserved = account.reserved_minor if account else 0
    if balance - reserved <= 0:
        issues.append("Add verified funds before enabling metered operations.")
    return BillingView(
        tenant_id=tenant_id,
        revision=account.revision if account else 0,
        balance_minor=balance,
        reserved_minor=reserved,
        available_minor=balance - reserved,
        subscription=subscription,
        funding_ready=not issues,
        readiness_issues=issues,
    )


async def lock_tenant(session: AsyncSession, tenant_id: UUID) -> Tenant:
    # Keep the cross-pool mutex compatible with FK key-share locks in the writer.
    row = await session.scalar(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update(key_share=True)
    )
    if row is None:
        raise NotFoundError()
    return row


async def account_for_write(session: AsyncSession, tenant_id: UUID) -> BillingAccount:
    account = await session.scalar(
        select(BillingAccount).where(BillingAccount.tenant_id == tenant_id)
    )
    if account is None:
        account = BillingAccount(
            id=uuid4(),
            tenant_id=tenant_id,
            revision=0,
            currency="INR",
            balance_minor=0,
            reserved_minor=0,
        )
        session.add(account)
        await session.flush()
    return account


async def receipt(
    session: AsyncSession, tenant_id: UUID, mutation_id: UUID, digest: str, actor_key: str
) -> BillingMutationView | None:
    row = await session.scalar(
        select(BillingMutation).where(
            BillingMutation.tenant_id == tenant_id, BillingMutation.mutation_id == mutation_id
        )
    )
    if row is None:
        return None
    if row.command_hash != digest or row.actor_key != actor_key:
        raise ConflictError()
    return BillingMutationView.model_validate(row.result)


def record_receipt(
    session: AsyncSession, tenant_id: UUID, digest: str, actor_key: str, result: BillingMutationView
) -> None:
    session.add(
        BillingMutation(
            id=uuid4(),
            tenant_id=tenant_id,
            mutation_id=result.mutation_id,
            command_hash=digest,
            actor_key=actor_key,
            result=result.model_dump(mode="json"),
        )
    )


def record_ledger(
    session: AsyncSession,
    account: BillingAccount,
    *,
    mutation_id: UUID,
    kind: str,
    amount: int,
    reason: str,
    request_id: UUID,
    actor_id: UUID | None = None,
    platform_user_id: UUID | None = None,
    reserved_delta: int = 0,
) -> UUID:
    if (
        not 0 <= account.balance_minor + amount <= MAX_MONEY
        or not 0 <= account.reserved_minor + reserved_delta <= account.balance_minor + amount
    ):
        raise InsufficientFundsError()
    account.balance_minor += amount
    account.reserved_minor += reserved_delta
    account.revision += 1
    identity = uuid4()
    session.add(
        BillingLedger(
            id=identity,
            tenant_id=account.tenant_id,
            revision=account.revision,
            mutation_id=mutation_id,
            kind=kind,
            amount_minor=amount,
            reserved_delta_minor=reserved_delta,
            balance_after_minor=account.balance_minor,
            reserved_after_minor=account.reserved_minor,
            reason=reason,
            actor_id=actor_id,
            platform_user_id=platform_user_id,
            request_id=request_id,
        )
    )
    return identity


class BillingService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        financial_sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._sessions = sessions
        self._financial_sessions = financial_sessions

    @asynccontextmanager
    async def _write(
        self, context: RequestContext
    ) -> AsyncGenerator[tuple[AsyncSession, AsyncSession]]:
        async with self._sessions.begin() as authority:
            await enter_tenant_scope(authority, context)
            await lock_tenant(authority, context.tenant_id)
            await enter_tenant_scope(authority, context)
            async with financial_transaction(self._financial_sessions) as writer:
                await set_tenant_scope(writer, context.tenant_id)
                await lock_financial_tenant(writer, context.tenant_id)
                yield writer, authority

    async def catalog(self, context: RequestContext) -> list[PlanView]:
        require_permission(context, Permission.BILLING_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            latest = (
                select(PlanVersion.key, func.max(PlanVersion.version).label("version"))
                .group_by(PlanVersion.key)
                .subquery()
            )
            rows = await session.scalars(
                select(PlanVersion)
                .join(
                    latest,
                    (PlanVersion.key == latest.c.key) & (PlanVersion.version == latest.c.version),
                )
                .order_by(PlanVersion.key)
                .limit(200)
            )
            return [plan_view(row) for row in rows]

    async def get(self, context: RequestContext) -> BillingView:
        require_permission(context, Permission.BILLING_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            return await billing_view(session, context.tenant_id)

    async def mutation(self, context: RequestContext, identity: UUID) -> BillingMutationView:
        require_permission(context, Permission.BILLING_READ)
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            row = await session.scalar(
                select(BillingMutation).where(
                    BillingMutation.tenant_id == context.tenant_id,
                    BillingMutation.mutation_id == identity,
                )
            )
            if row is None:
                raise NotFoundError()
            return BillingMutationView.model_validate(row.result)

    async def select_plan(
        self, context: RequestContext, command: SelectPlan
    ) -> BillingMutationView:
        require_permission(context, Permission.BILLING_MANAGE)
        async with self._write(context) as (session, authority):
            require_permission(context, Permission.BILLING_MANAGE)
            actor = (
                f"tenant:{context.actor_id}"
                if context.actor_id
                else f"platform:{context.platform_user_id}"
            )
            digest = command_hash(command)
            existing = await receipt(session, context.tenant_id, command.mutation_id, digest, actor)
            if existing:
                return existing
            account = await account_for_write(session, context.tenant_id)
            if account.revision != command.revision:
                raise RevisionConflictError()
            plan = await session.get(PlanVersion, command.plan_version_id)
            if plan is None:
                raise NotFoundError()
            latest = await session.scalar(
                select(func.max(PlanVersion.version)).where(PlanVersion.key == plan.key)
            )
            if latest != plan.version:
                raise RevisionConflictError()
            now = datetime.now(UTC)
            if account.period_end and account.period_end > now:
                raise SubscriptionLockedError()
            pending = await session.scalar(
                select(UsageReservation.id)
                .where(
                    UsageReservation.tenant_id == context.tenant_id,
                    UsageReservation.state.in_(("held", "running", "reconciliation")),
                )
                .limit(1)
            )
            if account.reserved_minor or pending:
                raise SubscriptionLockedError()
            definition = PlanDefinition.model_validate(plan.definition)
            entry = record_ledger(
                session,
                account,
                mutation_id=command.mutation_id,
                kind="subscription",
                amount=-definition.subscription_price_minor,
                reason=f"Subscription {plan.key} version {plan.version}",
                request_id=context.request_id,
                actor_id=context.actor_id,
                platform_user_id=context.platform_user_id,
            )
            account.plan_version_id = plan.id
            account.period_start = now
            account.period_end = next_month(now)
            result = BillingMutationView(
                mutation_id=command.mutation_id,
                revision=account.revision,
                kind="subscription",
                ledger_entry_id=entry,
            )
            record_receipt(session, context.tenant_id, digest, actor, result)
            audit(
                authority, context, "billing_subscription_selected", "billing_account", account.id
            )
            await authority.flush()
            await session.flush()
            return result

    async def ledger(
        self,
        context: RequestContext,
        cipher: SecretCipher,
        *,
        cursor: str | None = None,
        limit: int = 25,
        q: str | None = None,
        kind: LedgerKind | None = None,
    ) -> LedgerPage:
        require_permission(context, Permission.BILLING_READ)
        if limit not in {25, 50, 100}:
            raise InvalidBillingCursorError()
        binding = (
            f"billing-ledger:v1:{context.tenant_id}:{context.actor_id}:"
            f"{context.human_session_id}:{context.support_session_id}:"
            f"{context.role_id}:{context.authorization_revision}:{limit}:{q or ''}:{kind or ''}"
        )
        before: int | None = None
        if cursor:
            try:
                envelope = cast(dict[str, Any], json.loads(base64.urlsafe_b64decode(cursor)))
                key, encoded = envelope["key"], envelope["data"]
                if not isinstance(key, str) or not isinstance(encoded, str):
                    raise ValueError
                payload = cast(
                    dict[str, Any],
                    json.loads(cipher.decrypt(key, base64.urlsafe_b64decode(encoded), binding)),
                )
                if (
                    payload["expires"] < datetime.now(UTC).timestamp()
                    or not isinstance(payload["before"], int)
                    or payload["before"] < 1
                ):
                    raise ValueError
                before = payload["before"]
            except (ValueError, TypeError, KeyError, InvalidTag):
                raise InvalidBillingCursorError() from None
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            query = select(BillingLedger).where(BillingLedger.tenant_id == context.tenant_id)
            if kind:
                query = query.where(BillingLedger.kind == kind)
            search = contains(q, BillingLedger.reason)
            if search is not None:
                query = query.where(search)
            if before is not None:
                query = query.where(BillingLedger.revision < before)
            rows = list(
                await session.scalars(
                    query.order_by(BillingLedger.revision.desc()).limit(limit + 1)
                )
            )
            next_cursor = None
            if len(rows) > limit:
                key, encrypted = cipher.encrypt(
                    json.dumps(
                        {
                            "before": rows[limit - 1].revision,
                            "expires": (datetime.now(UTC) + timedelta(minutes=15)).timestamp(),
                        }
                    ).encode(),
                    binding,
                )
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(
                        {"key": key, "data": base64.urlsafe_b64encode(encrypted).decode()}
                    ).encode()
                ).decode()
            items = [
                LedgerEntryView.model_validate(
                    {field: getattr(row, field) for field in LedgerEntryView.model_fields}
                )
                for row in rows[:limit]
            ]
            return LedgerPage(items=items, next_cursor=next_cursor)


class PlatformBillingService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        financial_sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._sessions = sessions
        self._financial_sessions = financial_sessions

    async def catalog(self, context: PlatformContext) -> list[PlanView]:
        require_platform_permission(context, PlatformPermission.PRICING_MANAGE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            latest = (
                select(PlanVersion.key, func.max(PlanVersion.version).label("version"))
                .group_by(PlanVersion.key)
                .subquery()
            )
            rows = await session.scalars(
                select(PlanVersion)
                .join(
                    latest,
                    (PlanVersion.key == latest.c.key) & (PlanVersion.version == latest.c.version),
                )
                .order_by(PlanVersion.key)
                .limit(200)
            )
            return [plan_view(row) for row in rows]

    async def plan_receipt(self, context: PlatformContext, identity: UUID) -> PlanView:
        require_platform_permission(context, PlatformPermission.PRICING_MANAGE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await session.scalar(
                select(PlanVersion).where(
                    PlanVersion.mutation_id == identity,
                    PlanVersion.platform_user_id == context.user_id,
                )
            )
            if row is None:
                raise NotFoundError()
            return plan_view(row)

    async def tenant_billing(self, context: PlatformContext, tenant_id: UUID) -> BillingView:
        require_platform_permission(context, PlatformPermission.FINANCIAL_ADJUST)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                raise NotFoundError()
            await set_tenant_scope(session, tenant_id)
            return await billing_view(session, tenant_id)

    async def adjustment_receipt(
        self, context: PlatformContext, tenant_id: UUID, identity: UUID
    ) -> BillingMutationView:
        require_platform_permission(context, PlatformPermission.FINANCIAL_ADJUST)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            await set_tenant_scope(session, tenant_id)
            row = await session.scalar(
                select(BillingMutation).where(
                    BillingMutation.tenant_id == tenant_id,
                    BillingMutation.mutation_id == identity,
                    BillingMutation.actor_key == f"platform:{context.user_id}",
                )
            )
            if row is None:
                raise NotFoundError()
            result = BillingMutationView.model_validate(row.result)
            if result.kind != "adjustment":
                raise NotFoundError()
            return result

    @asynccontextmanager
    async def _write(
        self, context: PlatformContext, tenant_id: UUID | None = None
    ) -> AsyncGenerator[tuple[AsyncSession, AsyncSession]]:
        async with self._sessions.begin() as authority:
            await platform_scope(authority, context)
            if tenant_id:
                await lock_tenant(authority, tenant_id)
            else:
                await authority.execute(text("SELECT pg_advisory_xact_lock(734831742012)"))
            await platform_scope(authority, context)
            async with financial_transaction(self._financial_sessions) as writer:
                await writer.execute(
                    text(
                        "SELECT set_config('app.platform_user_id', :identity, true), set_"
                        "config('app.financial_platform_session_id', :session_id, true)"
                    ),
                    {"identity": str(context.user_id), "session_id": str(context.session_id)},
                )
                if tenant_id:
                    await set_tenant_scope(writer, tenant_id)
                    await lock_financial_tenant(writer, tenant_id)
                yield writer, authority

    async def publish_plan(self, context: PlatformContext, command: PublishPlan) -> PlanView:
        require_platform_permission(context, PlatformPermission.PRICING_MANAGE)
        async with self._write(context) as (session, authority):
            digest = command_hash(command)
            previous = await session.scalar(
                select(PlanVersion).where(PlanVersion.mutation_id == command.mutation_id)
            )
            if previous:
                if previous.command_hash != digest or previous.platform_user_id != context.user_id:
                    raise ConflictError()
                return plan_view(previous)
            current = (
                await session.scalar(
                    select(func.max(PlanVersion.version)).where(PlanVersion.key == command.key)
                )
                or 0
            )
            if current != command.expected_version:
                raise RevisionConflictError()
            if not current:
                count = (
                    await session.scalar(select(func.count(func.distinct(PlanVersion.key)))) or 0
                )
                if count >= 200:
                    raise ConflictError()
            row = PlanVersion(
                id=uuid4(),
                key=command.key,
                version=current + 1,
                definition=command.definition.model_dump(mode="json"),
                mutation_id=command.mutation_id,
                command_hash=digest,
                platform_user_id=context.user_id,
                request_id=context.request_id,
            )
            session.add(row)
            authority.add(
                PlatformAuditEvent(
                    id=uuid4(),
                    user_id=context.user_id,
                    action="billing_plan_published",
                    request_id=context.request_id,
                    resource_id=row.id,
                )
            )
            await authority.flush()
            await session.flush()
            return plan_view(row)

    async def adjust(
        self, context: PlatformContext, tenant_id: UUID, command: FinancialAdjustment
    ) -> BillingMutationView:
        require_platform_permission(context, PlatformPermission.FINANCIAL_ADJUST)
        async with self._write(context, tenant_id) as (session, authority):
            actor = f"platform:{context.user_id}"
            digest = command_hash(command)
            previous = await receipt(session, tenant_id, command.mutation_id, digest, actor)
            if previous:
                return previous
            account = await account_for_write(session, tenant_id)
            if account.revision != command.revision:
                raise RevisionConflictError()
            entry = record_ledger(
                session,
                account,
                mutation_id=command.mutation_id,
                kind="adjustment",
                amount=command.amount_minor,
                reason=command.reason,
                request_id=context.request_id,
                platform_user_id=context.user_id,
            )
            result = BillingMutationView(
                mutation_id=command.mutation_id,
                revision=account.revision,
                kind="adjustment",
                ledger_entry_id=entry,
            )
            record_receipt(session, tenant_id, digest, actor, result)
            authority.add(
                PlatformAuditEvent(
                    id=uuid4(),
                    user_id=context.user_id,
                    tenant_id=tenant_id,
                    action="billing_financial_adjustment",
                    request_id=context.request_id,
                    resource_id=entry,
                )
            )
            await authority.flush()
            await session.flush()
            return result
