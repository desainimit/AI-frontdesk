from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditAction
from app.audit.repository import AuditRepository
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext, Role
from app.auth.models import ApiCredential
from app.auth.tokens import issue_token
from app.database.rls import set_tenant_scope
from app.errors import ConflictError, NotFoundError
from app.tenants.models import Tenant
from app.users.models import User


class BootstrapTenant(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "name": "City Health Clinic",
                "email": "admin@cityhealth.com",
                "display_name": "Clinic Admin",
                "credential_days": 30,
            }
        },
    )
    tenant_id: UUID | None = Field(
        default=None,
        description="Optional tenant UUID. If omitted, a random UUID is automatically generated.",
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        description="Organization or clinic name",
        examples=["City Health Clinic"],
    )
    email: str = Field(
        min_length=3,
        max_length=254,
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
        description="Tenant administrator email",
        examples=["admin@cityhealth.com"],
    )
    display_name: str = Field(
        min_length=1,
        max_length=200,
        description="Tenant administrator display name",
        examples=["Clinic Admin"],
    )
    credential_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Initial API token validity period in days",
        examples=[30],
    )


@dataclass(frozen=True)
class BootstrapResult:
    tenant_id: UUID
    user_id: UUID
    credential_id: UUID
    token: SecretStr


class TenantBootstrapService:
    """Operator-only service. Never expose the migration session factory to the HTTP app."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, command: BootstrapTenant) -> BootstrapResult:
        async with self._sessions.begin() as session:
            return await self.create_in_session(session, command)

    async def create_in_session(
        self, session: AsyncSession, command: BootstrapTenant
    ) -> BootstrapResult:
        """Caller owns the transaction; permits atomic platform + tenant audit records."""
        tenant_id = command.tenant_id or uuid4()
        context = RequestContext(uuid4(), tenant_id, None, ActorType.SYSTEM)
        await set_tenant_scope(session, tenant_id)
        if await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id)):
            raise ConflictError()
        # Legacy contract: the workspace uses platform providers and is live at once.
        tenant = Tenant(
            id=tenant_id, name=command.name, service_state="live", requires_saas_activation=False
        )
        session.add(tenant)
        await session.flush()
        from app.rbac.service import seed_roles

        roles = await seed_roles(session, tenant.id)
        from app.configuration.service import seed_configuration

        await seed_configuration(session, tenant)
        user = User(
            id=uuid4(),
            tenant_id=tenant.id,
            email=command.email.lower(),
            display_name=command.display_name,
            role=Role.ADMIN,
            role_id=roles["owner"],
        )
        session.add(user)
        await session.flush()
        credential_id, token, digest = issue_token(tenant.id)
        session.add(
            ApiCredential(
                id=credential_id,
                tenant_id=tenant.id,
                user_id=user.id,
                token_digest=digest,
                expires_at=datetime.now(UTC) + timedelta(days=command.credential_days),
            )
        )
        audit = AuditService(AuditRepository(session, context))
        audit.record(context, AuditAction.TENANT_CREATED, "tenant", tenant.id)
        audit.record(context, AuditAction.USER_CREATED, "user", user.id)
        audit.record(context, AuditAction.CREDENTIAL_ISSUED, "api_credential", credential_id)
        return BootstrapResult(tenant.id, user.id, credential_id, token)

    async def revoke(self, tenant_id: UUID, credential_id: UUID) -> None:
        context = RequestContext(uuid4(), tenant_id, None, ActorType.SYSTEM)
        async with self._sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            credential = await session.scalar(
                select(ApiCredential)
                .where(
                    ApiCredential.tenant_id == tenant_id,
                    ApiCredential.id == credential_id,
                )
                .with_for_update()
            )
            if credential is None:
                raise NotFoundError()
            if credential.revoked_at is None:
                credential.revoked_at = datetime.now(UTC)
                AuditService(AuditRepository(session, context)).record(
                    context, AuditAction.CREDENTIAL_REVOKED, "api_credential", credential.id
                )
