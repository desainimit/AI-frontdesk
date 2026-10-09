from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.configuration.models import ConfigurationDraft
from app.core.database.search import contains
from app.core.errors import ConflictError, NotFoundError
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.service import validate_context
from app.tenants.bootstrap import BootstrapTenant, TenantBootstrapService
from app.tenants.models import Tenant
from app.tenants.schemas import TenantView


class CreateTenantCommand(BaseModel):
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


class TenantCreatedView(BaseModel):
    tenant_id: UUID
    user_id: UUID
    credential_id: UUID
    admin_token: str = Field(repr=False)


class TenantStatusCommand(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "is_active": True,
            }
        },
    )
    # Send either or both; an omitted field is left as it is.
    is_active: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def something_to_change(self) -> "TenantStatusCommand":
        if self.is_active is None and self.name is None:
            raise ValueError("Nothing to update")
        return self


async def platform_scope(session: AsyncSession, context: PlatformContext) -> None:
    await validate_context(session, context)
    await session.execute(
        text("SELECT set_config('app.platform_user_id', :identity, true)"),
        {"identity": str(context.user_id)},
    )


class PlatformTenantService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def list(
        self,
        context: PlatformContext,
        limit: int,
        offset: int,
        *,
        q: str | None = None,
        is_active: bool | None = None,
        service_state: str | None = None,
    ) -> list[TenantView]:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            query = select(Tenant)
            search = contains(q, Tenant.name)
            if search is not None:
                query = query.where(search)
            if is_active is not None:
                query = query.where(Tenant.is_active.is_(is_active))
            if service_state is not None:
                query = query.where(Tenant.service_state == service_state)
            tenants = (
                await session.scalars(
                    query.order_by(Tenant.created_at.desc(), Tenant.id).limit(limit).offset(offset)
                )
            ).all()
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id, action="tenants_viewed", request_id=context.request_id
                )
            )
            return [TenantView.model_validate(row) for row in tenants]

    async def create(
        self, context: PlatformContext, command: CreateTenantCommand
    ) -> TenantCreatedView:
        require_platform_permission(context, PlatformPermission.TENANT_CREATE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            bootstrap_command = BootstrapTenant(
                name=command.name,
                email=command.email,
                display_name=command.display_name,
                credential_days=command.credential_days,
            )
            result = await TenantBootstrapService(self._sessions).create_in_session(
                session, bootstrap_command
            )
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    action="tenant_created",
                    request_id=context.request_id,
                    resource_id=result.tenant_id,
                )
            )
            return TenantCreatedView(
                tenant_id=result.tenant_id,
                user_id=result.user_id,
                credential_id=result.credential_id,
                admin_token=result.token.get_secret_value(),
            )

    async def set_active(
        self, context: PlatformContext, tenant_id: UUID, command: TenantStatusCommand
    ) -> TenantView:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == tenant_id).with_for_update()
            )
            if tenant is None:
                raise NotFoundError()
            if command.name is not None and tenant.name != command.name:
                published = await session.scalar(
                    select(ConfigurationDraft.published_version).where(
                        ConfigurationDraft.tenant_id == tenant_id
                    )
                )
                if published is not None:
                    raise ConflictError()
                tenant.name = command.name
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        action="tenant_renamed",
                        request_id=context.request_id,
                        resource_id=tenant.id,
                    )
                )
            if command.is_active is not None and tenant.is_active != command.is_active:
                tenant.is_active = command.is_active
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        action="tenant_activated" if command.is_active else "tenant_deactivated",
                        request_id=context.request_id,
                        resource_id=tenant.id,
                    )
                )
            return TenantView.model_validate(tenant)
