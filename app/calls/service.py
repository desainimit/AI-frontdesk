from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditAction
from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.models import Call, PhoneNumber
from app.configuration.models import PublishedConfiguration
from app.configuration.schemas import OperationalConfiguration
from app.conversation.locales import LanguagePolicy
from app.database.rls import set_tenant_scope, verify_runtime_role
from app.errors import AuthorizationError, ConflictError, NotFoundError
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.tenants import platform_scope
from app.postcall.models import CallTranscript
from app.postcall.schemas import PostCallInput
from app.tenants.models import Tenant
from app.users.models import User
from app.workflows.intent import add_intent


class PhoneCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")


class PhoneView(PhoneCommand):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    is_active: bool


class CallView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    phone_number_id: UUID
    provider_call_id: str
    stream_sid: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    received_frames: int
    sent_frames: int
    end_reason: str | None


@dataclass(frozen=True)
class CallContext:
    tenant_id: UUID
    call_id: UUID
    provider_call_id: str
    request_id: UUID


@dataclass(frozen=True)
class CallBinding:
    """The provider connection pinned to a call; both None for a platform-account call."""

    integration_id: UUID | None
    credential_version_id: UUID | None


def audit(session: AsyncSession, context: CallContext, action: str) -> None:
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_type="system",
            actor_id=None,
            action=action,
            resource_type="call",
            resource_id=context.call_id,
            request_id=context.request_id,
            call_id=context.call_id,
        )
    )


class CallService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def assign(self, context: PlatformContext, command: PhoneCommand) -> PhoneView:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        try:
            async with self.sessions.begin() as session:
                await platform_scope(session, context)
                tenant = await session.scalar(
                    select(Tenant).where(Tenant.id == command.tenant_id, Tenant.is_active.is_(True))
                )
                if tenant is None:
                    raise NotFoundError()
                phone = PhoneNumber(tenant_id=command.tenant_id, number=command.number)
                session.add(phone)
                await session.flush()
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        action="phone_number_assigned",
                        request_id=context.request_id,
                        resource_id=phone.id,
                    )
                )
                return PhoneView.model_validate(phone)
        except IntegrityError:
            raise ConflictError("Phone number is already assigned") from None

    async def phones(self, context: PlatformContext, limit: int, offset: int) -> list[PhoneView]:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self.sessions.begin() as session:
            await platform_scope(session, context)
            rows = await session.scalars(
                select(PhoneNumber).order_by(PhoneNumber.id).limit(limit).offset(offset)
            )
            return [PhoneView.model_validate(row) for row in rows]

    async def tenant_for_number(self, number: str) -> UUID | None:
        """Candidate tenant for an unauthenticated webhook. Untrusted until its signature
        verifies against that tenant's own connection; reveals only the routing owner."""
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await session.execute(
                text("SELECT set_config('app.inbound_number', :n, true)"), {"n": number}
            )
            return await session.scalar(
                select(PhoneNumber.tenant_id).where(PhoneNumber.number == number)
            )

    async def binding(self, tenant_id: UUID, call_id: UUID) -> CallBinding | None:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, tenant_id)
            row = (
                await session.execute(
                    select(Call.integration_id, Call.credential_version_id).where(
                        Call.tenant_id == tenant_id, Call.id == call_id
                    )
                )
            ).first()
            return CallBinding(row[0], row[1]) if row else None

    async def receive(
        self,
        number: str,
        sid: str,
        request_id: UUID,
        binding: CallBinding | None = None,
    ) -> CallContext:
        # Only invoked after provider signature and account validation.
        pinned = binding or CallBinding(None, None)
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await session.execute(
                text("SELECT set_config('app.inbound_number', :n, true)"), {"n": number}
            )
            phone = await session.scalar(
                select(PhoneNumber).where(
                    PhoneNumber.number == number, PhoneNumber.is_active.is_(True)
                )
            )
            if phone is None:
                raise NotFoundError()
            # A number proven against one connection is served by that connection only.
            if phone.integration_id != pinned.integration_id:
                raise AuthorizationError()
            await set_tenant_scope(session, phone.tenant_id)
            await self._active(session, phone.tenant_id, live=True)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:sid, 0))"), {"sid": sid}
            )
            call = await session.scalar(select(Call).where(Call.provider_call_id == sid))
            if call is None:
                from app.configuration.service import published_for_tenant

                operational = await published_for_tenant(session, phone.tenant_id)
                if operational is not None and not operational[1].voice.enabled:
                    raise AuthorizationError()
                from app.configuration.resolution import resolve_configuration

                selected = None
                if operational is not None:
                    routes = [
                        (identity, scope)
                        for identity, scope in operational[1].scopes.channels.items()
                        if scope.kind == "voice"
                    ]
                    channel_id = next(
                        (
                            identity
                            for identity, scope in routes
                            if scope.phone_number_id == phone.id
                        ),
                        None,
                    )
                    if routes and channel_id is None:
                        raise AuthorizationError()
                    selected = resolve_configuration(
                        operational[1], operational[0], channel_id=channel_id
                    )
                    await self._scope_resources_active(
                        session, selected.agent_id, selected.channel_id
                    )
                call = Call(
                    id=uuid4(),
                    tenant_id=phone.tenant_id,
                    phone_number_id=phone.id,
                    provider_call_id=sid,
                    integration_id=pinned.integration_id,
                    credential_version_id=pinned.credential_version_id,
                    configuration_version=operational[0] if operational else None,
                    location_id=selected.location_id if selected else None,
                    agent_id=selected.agent_id if selected else None,
                    channel_id=selected.channel_id if selected else None,
                )
                session.add(call)
                await session.flush()
                audit(
                    session, CallContext(call.tenant_id, call.id, sid, request_id), "call_received"
                )
            elif (
                call.phone_number_id != phone.id
                or call.integration_id != pinned.integration_id
                or call.status != "received"
                or datetime.now(UTC) - call.created_at > timedelta(minutes=5)
            ):
                raise ConflictError("Call is no longer awaiting a stream")
            return CallContext(call.tenant_id, call.id, sid, request_id)

    async def operational_configuration(
        self, context: CallContext
    ) -> tuple[int, OperationalConfiguration] | None:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            call = await session.scalar(
                select(Call).where(
                    Call.tenant_id == context.tenant_id,
                    Call.id == context.call_id,
                    Call.provider_call_id == context.provider_call_id,
                )
            )
            if call is None:
                raise AuthorizationError()
            if call.configuration_version is None:
                return None
            row = await session.scalar(
                select(PublishedConfiguration).where(
                    PublishedConfiguration.tenant_id == context.tenant_id,
                    PublishedConfiguration.version == call.configuration_version,
                )
            )
            if row is None:
                raise AuthorizationError()
            from app.configuration.resolution import resolve_configuration

            if call.status == "received":
                await self._scope_resources_active(session, call.agent_id, call.channel_id)
            resolved = resolve_configuration(
                OperationalConfiguration.model_validate(row.document),
                row.version,
                location_id=call.location_id,
                agent_id=call.agent_id,
                channel_id=call.channel_id,
            )
            return row.version, resolved.document

    async def _scope_resources_active(
        self, session: AsyncSession, agent_id: UUID | None, channel_id: UUID | None
    ) -> None:
        from app.configuration.models import ConfigurationResource

        for kind, identity in (("agent", agent_id), ("channel", channel_id)):
            if identity is not None:
                resource = await session.scalar(
                    select(ConfigurationResource)
                    .where(
                        ConfigurationResource.id == identity,
                        ConfigurationResource.kind == kind,
                        ConfigurationResource.is_active.is_(True),
                    )
                    .with_for_update(read=True)
                )
                if resource is None:
                    raise AuthorizationError()

    async def _active(self, session: AsyncSession, tenant_id: UUID, *, live: bool = False) -> None:
        query = select(Tenant.id).where(Tenant.id == tenant_id, Tenant.is_active.is_(True))
        if live:
            # New calls need explicit activation; a workspace in setup takes none.
            query = query.where(Tenant.service_state == "live")
        if await session.scalar(query) is None:
            raise AuthorizationError()

    async def start(self, context: CallContext, stream_sid: str) -> None:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            await self._active(session, context.tenant_id)
            call = await self._locked(session, context)
            await self._scope_resources_active(session, call.agent_id, call.channel_id)
            phone = await session.get(PhoneNumber, call.phone_number_id)
            if (
                call.status != "received"
                or phone is None
                or not phone.is_active
                or datetime.now(UTC) - call.created_at > timedelta(minutes=5)
            ):
                raise AuthorizationError()
            call.status = "active"
            call.stream_sid = stream_sid
            call.started_at = datetime.now(UTC)
            audit(session, context, "call_started")

    async def _locked(self, session: AsyncSession, context: CallContext) -> Call:
        call = await session.scalar(
            select(Call)
            .where(
                Call.id == context.call_id,
                Call.tenant_id == context.tenant_id,
                Call.provider_call_id == context.provider_call_id,
            )
            .with_for_update()
        )
        if call is None:
            raise NotFoundError()
        return call

    async def finish(
        self,
        context: CallContext,
        status: str,
        reason: str,
        received: int = 0,
        sent: int = 0,
        *,
        post_call_input: PostCallInput | None = None,
    ) -> None:
        if status not in {"completed", "disconnected", "failed"}:
            raise ValueError("Invalid terminal status")
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            call = await self._locked(session, context)
            # Callback and socket cleanup may race; preserve the first terminal outcome.
            call.received_frames = max(call.received_frames, received)
            call.sent_frames = max(call.sent_frames, sent)
            if post_call_input is not None:
                await session.execute(
                    insert(CallTranscript)
                    .values(
                        tenant_id=context.tenant_id,
                        call_id=call.id,
                        snapshot=post_call_input.model_dump(mode="json"),
                    )
                    .on_conflict_do_update(
                        index_elements=["tenant_id", "call_id"],
                        set_={"snapshot": post_call_input.model_dump(mode="json")},
                    )
                )
            if call.ended_at is None:
                call.status = status
                call.end_reason = reason
                call.ended_at = datetime.now(UTC)
                audit(session, context, "call_ended")
                add_intent(session, context.tenant_id, "post_call", call.id)

    async def provider_status(
        self,
        number: str,
        sid: str,
        status: str,
        request_id: UUID,
        integration_id: UUID | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await session.execute(
                text("SELECT set_config('app.inbound_number', :n, true)"), {"n": number}
            )
            phone = await session.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
            if phone is None:
                raise NotFoundError()
            await set_tenant_scope(session, phone.tenant_id)
            call = await session.scalar(select(Call).where(Call.provider_call_id == sid))
            # The authenticated account must be the one the call was accepted on.
            if call is None or call.integration_id != integration_id:
                raise NotFoundError()
            context = CallContext(call.tenant_id, call.id, sid, request_id)
        await self.finish(
            context, "completed" if status == "completed" else "failed", "provider_" + status
        )

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        call_id: UUID | None = None,
        *,
        status: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> list[CallView]:
        require_permission(context, Permission.CALL_READ)
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            await self._active(session, context.tenant_id)
            if context.actor_type == ActorType.PLATFORM_SUPPORT:
                from app.platform.support import validate_support_context

                await validate_support_context(session, context)
            elif (
                await session.scalar(
                    select(User.id).where(
                        User.id == context.actor_id,
                        User.is_active.is_(True),
                        User.role == context.role,
                    )
                )
                is None
            ):
                raise AuthorizationError()
            query = select(Call).where(Call.tenant_id == context.tenant_id)
            if call_id is not None:
                query = query.where(Call.id == call_id)
            if status is not None:
                query = query.where(Call.status == status)
            if created_from is not None:
                query = query.where(Call.created_at >= created_from)
            if created_to is not None:
                query = query.where(Call.created_at <= created_to)
            rows = await session.scalars(
                query.order_by(Call.created_at.desc(), Call.id).limit(limit).offset(offset)
            )
            result = [CallView.model_validate(row) for row in rows]
            session.add(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    actor_type=context.actor_type,
                    actor_id=context.actor_id,
                    platform_user_id=context.platform_user_id,
                    support_session_id=context.support_session_id,
                    action="calls_viewed",
                    resource_type="call",
                    resource_id=call_id,
                    request_id=context.request_id,
                    call_id=call_id,
                )
            )
            return result

    async def language_policy(
        self, context: CallContext, default_language: str = "en"
    ) -> LanguagePolicy:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            await self._active(session, context.tenant_id)
            call = await self._locked(session, context)
            if call.status != "active":
                raise AuthorizationError()
            tenant = await session.get(Tenant, context.tenant_id)
            if tenant is None:
                raise AuthorizationError()
            return LanguagePolicy.model_validate(
                tenant.language_policy or {"default_language": default_language}
            )

    async def transfer_number(self, context: CallContext) -> str | None:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            await self._active(session, context.tenant_id)
            call = await self._locked(session, context)
            if call.status != "active":
                raise AuthorizationError()
            tenant = await session.get(Tenant, context.tenant_id)
            inbound = await session.get(PhoneNumber, call.phone_number_id)
            if tenant is None or inbound is None:
                raise AuthorizationError()
            number = tenant.human_transfer_number
            return number if number and number != inbound.number else None

    async def record_safety_event(self, context: CallContext, action: AuditAction) -> None:
        if action not in {
            AuditAction.EMERGENCY_DETECTED,
            AuditAction.HUMAN_TRANSFER_REQUESTED,
            AuditAction.HUMAN_TRANSFER_REDIRECTED,
            AuditAction.HUMAN_TRANSFER_FAILED,
        }:
            raise ValueError("Unsupported safety event")
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            call = await self._locked(session, context)
            if call.status != "active":
                raise AuthorizationError()
            audit(session, context, action.value)
