from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database.rls import set_tenant_scope
from app.core.database.search import contains
from app.core.errors import NotFoundError
from app.identity.sessions import platform_scope
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.settings.models import TenantSettings
from app.tenants.models import Tenant
from app.tenants.schemas import TenantView


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
                tenant.name = command.name
                # The clinic name in settings is the same name; keep the two together.
                await set_tenant_scope(session, tenant_id)
                settings = await session.scalar(
                    select(TenantSettings)
                    .where(TenantSettings.tenant_id == tenant_id)
                    .with_for_update()
                )
                if settings is not None:
                    clinic = {**settings.document["clinic"], "name": command.name}
                    settings.document = {**settings.document, "clinic": clinic}
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
