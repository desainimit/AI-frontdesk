"""Tenant-scoped support access that never creates or impersonates a tenant user."""

import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActorType, RequestContext
from app.auth.policies import SUPPORT_PERMISSIONS, Permission
from app.auth.tokens import digest_token
from app.database.rls import set_tenant_scope, verify_runtime_role
from app.errors import AuthenticationError, AuthorizationError, NotFoundError
from app.platform.models import PlatformAuditEvent, PlatformSession, PlatformUser, SupportSession
from app.platform.policies import (
    PlatformContext,
    PlatformPermission,
    PlatformRole,
    require_platform_permission,
)
from app.platform.service import validate_context
from app.tenants.models import Tenant

SUPPORT_CAPABILITIES = SUPPORT_PERMISSIONS

# Route templates are intentional. Unknown and newly added routes are denied by default.
SUPPORT_ROUTES: dict[tuple[str, str], Permission | None] = {
    ("GET", "/api/v1/support/me"): None,
    ("POST", "/api/v1/support/exit"): None,
    ("GET", "/api/v1/tenants/current"): Permission.TENANT_READ,
    ("PUT", "/api/v1/tenants/current/languages"): Permission.TENANT_UPDATE,
    ("PUT", "/api/v1/tenants/current/transfer"): Permission.TENANT_UPDATE,
    ("PUT", "/api/v1/tenants/current/verification"): Permission.TENANT_UPDATE,
    ("PATCH", "/api/v1/tenants/current"): Permission.TENANT_UPDATE,
    ("GET", "/api/v1/users"): Permission.USER_READ,
    ("POST", "/api/v1/users/invitations"): Permission.USER_INVITE,
    ("POST", "/api/v1/users/{user_id}/invitations"): Permission.USER_INVITE,
    ("PATCH", "/api/v1/users/{user_id}"): Permission.USER_MANAGE,
    ("POST", "/api/v1/users/{user_id}/revoke-sessions"): Permission.USER_MANAGE,
    ("GET", "/api/v1/audit-events"): Permission.AUDIT_READ,
    ("GET", "/api/v1/locations"): Permission.DIRECTORY_READ,
    ("POST", "/api/v1/locations"): Permission.DIRECTORY_WRITE,
    ("GET", "/api/v1/providers"): Permission.DIRECTORY_READ,
    ("POST", "/api/v1/providers"): Permission.DIRECTORY_WRITE,
    ("GET", "/api/v1/providers/{provider_id}/availability"): Permission.DIRECTORY_READ,
    ("GET", "/api/v1/providers/{provider_id}/slots"): Permission.DIRECTORY_READ,
    ("POST", "/api/v1/providers/{provider_id}/availability"): Permission.DIRECTORY_WRITE,
    ("GET", "/api/v1/patients"): Permission.PATIENT_READ,
    ("POST", "/api/v1/patients"): Permission.PATIENT_WRITE,
    ("GET", "/api/v1/patients/{patient_id}"): Permission.PATIENT_READ,
    ("PUT", "/api/v1/patients/{patient_id}/verification"): Permission.PATIENT_WRITE,
    ("GET", "/api/v1/appointments"): Permission.APPOINTMENT_READ,
    ("POST", "/api/v1/appointments"): Permission.APPOINTMENT_WRITE,
    ("GET", "/api/v1/appointments/{appointment_id}"): Permission.APPOINTMENT_READ,
    ("POST", "/api/v1/appointments/{appointment_id}/cancel"): Permission.APPOINTMENT_WRITE,
    ("POST", "/api/v1/appointments/{appointment_id}/reschedule"): Permission.APPOINTMENT_WRITE,
    ("GET", "/api/v1/knowledge/documents"): Permission.KNOWLEDGE_READ,
    ("POST", "/api/v1/knowledge/documents"): Permission.KNOWLEDGE_WRITE,
    ("GET", "/api/v1/knowledge/documents/{document_id}/versions"): Permission.KNOWLEDGE_READ,
    ("POST", "/api/v1/knowledge/documents/{document_id}/versions"): Permission.KNOWLEDGE_WRITE,
    ("POST", "/api/v1/knowledge/documents/{document_id}/approve"): Permission.KNOWLEDGE_WRITE,
    ("POST", "/api/v1/knowledge/documents/{document_id}/archive"): Permission.KNOWLEDGE_WRITE,
    ("POST", "/api/v1/knowledge/search"): Permission.KNOWLEDGE_READ,
    ("GET", "/api/v1/calls"): Permission.CALL_READ,
    ("GET", "/api/v1/calls/{call_id}"): Permission.CALL_READ,
    ("POST", "/api/v1/calls/{call_id}/patient-access"): Permission.PATIENT_WRITE,
    ("GET", "/api/v1/calls/{call_id}/post-call"): Permission.CALL_READ,
    ("GET", "/api/v1/follow-ups"): Permission.CALL_READ,
    ("POST", "/api/v1/follow-ups/{event_id}/acknowledge"): Permission.FOLLOW_UP_MANAGE,
    ("GET", "/api/v1/analytics/calls"): Permission.CALL_READ,
    ("GET", "/api/v1/integration-providers"): Permission.INTEGRATION_READ,
    ("GET", "/api/v1/integrations"): Permission.INTEGRATION_READ,
    ("POST", "/api/v1/integrations"): Permission.INTEGRATION_MANAGE,
    ("GET", "/api/v1/integrations/{integration_id}"): Permission.INTEGRATION_READ,
    ("PATCH", "/api/v1/integrations/{integration_id}"): Permission.INTEGRATION_MANAGE,
    (
        "POST",
        "/api/v1/integrations/{integration_id}/credential-versions",
    ): Permission.INTEGRATION_MANAGE,
    ("POST", "/api/v1/integrations/{integration_id}/verify"): Permission.INTEGRATION_MANAGE,
    (
        "POST",
        "/api/v1/integrations/{integration_id}/activate-version",
    ): Permission.INTEGRATION_MANAGE,
    ("POST", "/api/v1/integrations/{integration_id}/disconnect"): Permission.INTEGRATION_MANAGE,
    ("GET", "/api/v1/integrations/{integration_id}/phone-numbers"): Permission.INTEGRATION_MANAGE,
    ("POST", "/api/v1/integrations/{integration_id}/phone-numbers"): Permission.INTEGRATION_MANAGE,
    ("GET", "/api/v1/tenants/current/readiness"): Permission.INTEGRATION_READ,
    ("POST", "/api/v1/tenants/current/activate"): Permission.TENANT_ACTIVATE,
    ("POST", "/api/v1/tenants/current/deactivate"): Permission.TENANT_ACTIVATE,
}


class EnterSupportCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    tenant_id: UUID
    # The reason is the one thing always required: it is what the audit log shows.
    reason: str = Field(min_length=3, max_length=500)
    # Omitted means full access. A narrower list is still honoured when given.
    capabilities: list[Permission] | None = Field(
        default=None, min_length=1, max_length=len(SUPPORT_CAPABILITIES)
    )
    # Accepted for older clients; no longer required for any capability.
    confirm_sensitive: bool = False
    # Administrative tenant entry is bounded independently of the platform session.
    duration_minutes: int | None = Field(default=30, ge=1, le=30)

    @model_validator(mode="after")
    def validate_capabilities(self) -> "EnterSupportCommand":
        if self.duration_minutes is None:
            self.duration_minutes = 30
        requested = self.granted
        if (self.capabilities is not None and len(set(requested)) != len(requested)) or not set(
            requested
        ) <= SUPPORT_CAPABILITIES:
            raise ValueError("Unsupported or duplicate support capability")
        return self

    @property
    def granted(self) -> list[Permission]:
        if self.capabilities is not None:
            return self.capabilities
        return sorted(SUPPORT_CAPABILITIES)


class SupportSessionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    platform_user_id: UUID
    capabilities: list[str]
    expires_at: datetime


class EnterSupportView(SupportSessionView):
    access_token: str = Field(repr=False)
    token_type: str = "bearer"


def parse_support_locator(token: str) -> tuple[UUID, UUID]:
    try:
        prefix, tenant, identity, secret = token.split(".")
        if prefix != "fds1" or len(secret) != 43 or len(token) > 150:
            raise ValueError
        return UUID(tenant), UUID(identity)
    except ValueError:
        raise AuthenticationError() from None


async def validate_support_context(
    session: AsyncSession, context: RequestContext
) -> SupportSession:
    """Recheck identity, scope and revocation inside each authorized database operation."""
    if context.actor_type != ActorType.PLATFORM_SUPPORT or context.support_session_id is None:
        raise AuthenticationError()
    row = await session.scalar(
        select(SupportSession)
        .join(PlatformSession, SupportSession.platform_session_id == PlatformSession.id)
        .join(PlatformUser, SupportSession.platform_user_id == PlatformUser.id)
        .join(Tenant, SupportSession.tenant_id == Tenant.id)
        .where(
            SupportSession.id == context.support_session_id,
            SupportSession.tenant_id == context.tenant_id,
            SupportSession.platform_user_id == context.platform_user_id,
            SupportSession.platform_session_id == context.platform_session_id,
            SupportSession.revoked_at.is_(None),
            SupportSession.expires_at > datetime.now(UTC),
            PlatformSession.user_id == SupportSession.platform_user_id,
            PlatformSession.revoked_at.is_(None),
            PlatformSession.expires_at > datetime.now(UTC),
            PlatformSession.mfa_verified_at.is_not(None),
            PlatformUser.is_active.is_(True),
            PlatformUser.role == PlatformRole.SUPER_ADMIN,
            Tenant.is_active.is_(True),
        )
        .with_for_update(read=True, of=SupportSession)
    )
    if row is None or frozenset(row.capabilities) != context.capabilities:
        raise AuthenticationError()
    return row


class SupportAccessService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def enter(
        self, context: PlatformContext, command: EnterSupportCommand
    ) -> EnterSupportView:
        require_platform_permission(context, PlatformPermission.SUPPORT_ENTER)
        async with self._sessions.begin() as session:
            await validate_context(session, context)
            await set_tenant_scope(session, command.tenant_id)
            tenant = await session.scalar(
                select(Tenant.id).where(Tenant.id == command.tenant_id, Tenant.is_active.is_(True))
            )
            if tenant is None:
                raise NotFoundError()
            parent = await session.get(PlatformSession, context.session_id)
            if parent is None:
                raise AuthenticationError()
            identity = uuid4()
            token = f"fds1.{tenant}.{identity}.{secrets.token_urlsafe(32)}"
            # Never outlives the platform sign-in; ends with it, or on logout or exit.
            expires = parent.expires_at
            if command.duration_minutes is not None:
                expires = min(
                    datetime.now(UTC) + timedelta(minutes=command.duration_minutes), expires
                )
            row = SupportSession(
                id=identity,
                tenant_id=tenant,
                platform_user_id=context.user_id,
                platform_session_id=context.session_id,
                token_digest=digest_token(token),
                reason=command.reason.strip(),
                capabilities=[item.value for item in command.granted],
                expires_at=expires,
            )
            session.add(row)
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    action="support_enter",
                    request_id=context.request_id,
                    resource_id=identity,
                    tenant_id=tenant,
                    support_session_id=identity,
                )
            )
            return EnterSupportView(
                id=identity,
                tenant_id=tenant,
                platform_user_id=context.user_id,
                capabilities=[item.value for item in command.granted],
                expires_at=expires,
                access_token=token,
            )

    async def authenticate(self, token: str, request_id: UUID) -> RequestContext:
        tenant_id, identity = parse_support_locator(token)
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, tenant_id)
            row = await session.scalar(
                select(SupportSession).where(
                    SupportSession.id == identity, SupportSession.tenant_id == tenant_id
                )
            )
            matches = hmac.compare_digest(
                row.token_digest if row else "0" * 64, digest_token(token)
            )
            if not matches or row is None:
                raise AuthenticationError()
            context = RequestContext(
                request_id=request_id,
                tenant_id=tenant_id,
                actor_id=None,
                actor_type=ActorType.PLATFORM_SUPPORT,
                platform_user_id=row.platform_user_id,
                platform_session_id=row.platform_session_id,
                support_session_id=row.id,
                support_expires_at=row.expires_at,
                capabilities=frozenset(row.capabilities),
            )
            await validate_support_context(session, context)
            return context

    async def action(
        self, context: RequestContext, method: str, route: str, requested_tenant: UUID | None = None
    ) -> None:
        known_route = (method, route) in SUPPORT_ROUTES
        permission = SUPPORT_ROUTES.get((method, route))
        allowed = (
            known_route
            and (requested_tenant is None or requested_tenant == context.tenant_id)
            and (permission is None or permission.value in context.capabilities)
        )
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            await validate_support_context(session, context)
            session.add(
                PlatformAuditEvent(
                    user_id=context.platform_user_id,
                    action="support_action_attempt" if allowed else "support_action_denied",
                    request_id=context.request_id,
                    tenant_id=context.tenant_id,
                    support_session_id=context.support_session_id,
                    http_method=method[:10],
                    route=route[:200],
                )
            )
        if not allowed:
            raise AuthorizationError()

    async def exit(self, context: RequestContext) -> None:
        async with self._sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            row = await validate_support_context(session, context)
            row.revoked_at = datetime.now(UTC)
            session.add(
                PlatformAuditEvent(
                    user_id=context.platform_user_id,
                    action="support_exit",
                    request_id=context.request_id,
                    tenant_id=context.tenant_id,
                    support_session_id=context.support_session_id,
                )
            )

    async def revoke(self, context: PlatformContext, tenant_id: UUID, identity: UUID) -> None:
        require_platform_permission(context, PlatformPermission.SUPPORT_REVOKE)
        async with self._sessions.begin() as session:
            await validate_context(session, context)
            await set_tenant_scope(session, tenant_id)
            row = await session.scalar(
                select(SupportSession)
                .where(SupportSession.tenant_id == tenant_id, SupportSession.id == identity)
                .with_for_update()
            )
            if row is None:
                raise NotFoundError()
            if row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        action="support_revoked",
                        request_id=context.request_id,
                        tenant_id=tenant_id,
                        support_session_id=identity,
                    )
                )
