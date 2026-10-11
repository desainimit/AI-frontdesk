from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appointments.repository import AppointmentRepository
from app.audit.repository import AuditRepository
from app.auth.context import ActorType, RequestContext
from app.clinic.repository import ScheduleRepository
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import AuthenticationError, AuthorizationError
from app.events.idempotency import IdempotencyRepository
from app.events.outbox import OutboxRepository
from app.knowledge.repository import KnowledgeRepository
from app.locations.repository import LocationRepository
from app.notifications.repository import MessageRepository
from app.patients.repository import PatientRepository
from app.providers.repository import ProviderRepository
from app.tenants.models import Tenant
from app.tenants.repository import TenantRepository
from app.users.models import User
from app.users.repository import UserRepository


@dataclass(frozen=True)
class Transaction:
    tenants: TenantRepository
    users: UserRepository
    audit: AuditRepository
    knowledge: KnowledgeRepository
    locations: LocationRepository
    providers: ProviderRepository
    patients: PatientRepository
    schedule: ScheduleRepository
    appointments: AppointmentRepository
    idempotency: IdempotencyRepository
    outbox: OutboxRepository
    messages: MessageRepository


async def enter_tenant_scope(session: AsyncSession, context: RequestContext) -> None:
    """Scope the transaction to the tenant and recheck the caller inside it.

    Membership, role, session revocation and call bindings are re-read here so a
    request that waited for a connection cannot act on a stale authorization.
    """
    await verify_runtime_role(session)
    await set_tenant_scope(session, context.tenant_id)
    if context.actor_type == ActorType.USER:
        # Recheck membership inside the actual operation, including role changes.
        authorized = await session.scalar(
            select(User)
            .join(Tenant, User.tenant_id == Tenant.id)
            .where(
                User.id == context.actor_id,
                User.tenant_id == context.tenant_id,
                User.is_active.is_(True),
                Tenant.is_active.is_(True),
            )
            .with_for_update(read=True, of=User)
        )
        if authorized is None:
            if context.session_id is not None:
                raise AuthenticationError()
            raise AuthorizationError()
        if context.session_id is not None:
            from app.identity.sessions import validate_user_session

            await validate_user_session(session, context, authorized)
        if context.role_id is not None:
            from app.rbac.service import bind_user_permissions

            current = await bind_user_permissions(session, context, authorized, lock=True)
            if (
                current.role_id != context.role_id
                or current.authorization_revision != context.authorization_revision
                or current.capabilities != context.capabilities
            ):
                raise AuthorizationError()
    if context.actor_type == ActorType.PLATFORM_SUPPORT:
        from app.identity.sessions import validate_support_context

        await validate_support_context(session, context)
    if context.actor_type == ActorType.CALLER:
        from app.calls.access import active_call
        from app.calls.service import CallContext

        if context.conversation_id is not None:
            from app.messaging.models import Conversation

            # A patient writing in: the thread must be this clinic's, still open, and
            # already tied to the patient they claim to be.
            conversation = await session.scalar(
                select(Conversation)
                .join(Tenant, Tenant.id == Conversation.tenant_id)
                .where(
                    Conversation.id == context.conversation_id,
                    Conversation.tenant_id == context.tenant_id,
                    Conversation.status == "open",
                    Tenant.is_active.is_(True),
                )
                .with_for_update(read=True, of=Conversation)
            )
            if conversation is None or (
                context.patient_id is not None and conversation.patient_id != context.patient_id
            ):
                raise AuthorizationError()
            return
        if context.call_id is None or context.provider_call_id is None:
            raise AuthorizationError()
        call = await active_call(
            session,
            CallContext(
                context.tenant_id,
                context.call_id,
                context.provider_call_id,
                context.request_id,
            ),
        )
        if context.patient_id is not None and (
            call.verified_patient_id != context.patient_id
            or call.verification_expires_at is None
            or call.verification_expires_at <= datetime.now(UTC)
        ):
            raise AuthorizationError()


class UnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @asynccontextmanager
    async def transaction(self, context: RequestContext) -> AsyncGenerator[Transaction]:
        # Fresh session per operation; nothing is kept on this reusable UoW instance.
        async with self._sessions.begin() as session:
            await enter_tenant_scope(session, context)
            yield Transaction(
                tenants=TenantRepository(session, context),
                users=UserRepository(session, context),
                audit=AuditRepository(session, context),
                knowledge=KnowledgeRepository(session),
                locations=LocationRepository(session, context),
                providers=ProviderRepository(session, context),
                patients=PatientRepository(session, context),
                schedule=ScheduleRepository(session, context),
                appointments=AppointmentRepository(session, context),
                idempotency=IdempotencyRepository(session, context),
                outbox=OutboxRepository(session, context),
                messages=MessageRepository(session, context),
            )
