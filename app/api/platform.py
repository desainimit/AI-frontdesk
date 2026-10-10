from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from app.api.dependencies import SessionToken, get_resources
from app.api.tenancy import Cursor, DatabaseDependency, Limit, Search
from app.core.database.pages import Page, decode_cursor, page
from app.core.security.rate_limit import enforce_rate_limit
from app.identity.invitations import InvitationService
from app.identity.schemas import InvitationView, OpenWorkspace, SupportVisit
from app.identity.service import IdentityService
from app.identity.sessions import authenticate_platform
from app.platform.onboarding import (
    OnboardingDefaults,
    OnboardingDraft,
    OnboardingView,
    TenantOnboardingService,
    UpdateOnboardingDraft,
    onboarding_defaults,
)
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.tenants import PlatformTenantService, TenantStatusCommand
from app.tenants.schemas import TenantView

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])

PLATFORM_REQUESTS_PER_MINUTE = 120


async def get_platform_context(
    request: Request, token: SessionToken, database: DatabaseDependency
) -> PlatformContext:
    """The signed-in platform administrator; anyone else is refused."""
    context = await authenticate_platform(
        database.session_factory, token, UUID(request.state.request_id)
    )
    await enforce_rate_limit(
        get_resources(request).redis,
        enabled=request.app.state.rate_limits,
        scope="platform_api",
        identity=str(context.user_id),
        limit=PLATFORM_REQUESTS_PER_MINUTE,
    )
    return context


PlatformIdentity = Annotated[PlatformContext, Depends(get_platform_context)]


@router.post("/workspaces/{tenant_id}/open")
async def open_workspace(
    tenant_id: UUID,
    command: OpenWorkspace,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> SupportVisit:
    """Start working inside a workspace for a stated reason; ends after 30 minutes."""
    return await IdentityService(
        database.session_factory, request.app.state.settings
    ).open_workspace(context, tenant_id, command)


@router.get("/tenants")
async def tenants(
    context: PlatformIdentity,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    q: Search = None,
    is_active: bool | None = None,
    service_state: Literal["setup", "live"] | None = None,
) -> Page[TenantView]:
    offset = decode_cursor(cursor)
    rows = await PlatformTenantService(database.session_factory).list(
        context, limit + 1, offset, q=q, is_active=is_active, service_state=service_state
    )
    return page(rows, limit, offset)


@router.patch("/tenants/{tenant_id}")
async def set_tenant_active(
    tenant_id: UUID,
    command: TenantStatusCommand,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> TenantView:
    return await PlatformTenantService(database.session_factory).set_active(
        context, tenant_id, command
    )


@router.post("/tenants/{tenant_id}/admin-invitations", status_code=201)
async def invite_tenant_admin(
    tenant_id: UUID,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> InvitationView:
    return await InvitationService(
        database.session_factory, request.app.state.settings
    ).invite_initial_admin(context, tenant_id, idempotency_key)


@router.get("/tenant-onboardings/defaults")
async def tenant_onboarding_defaults(context: PlatformIdentity) -> OnboardingDefaults:
    require_platform_permission(context, PlatformPermission.TENANT_CREATE)
    return onboarding_defaults()


@router.post("/tenant-onboardings", status_code=201)
async def create_onboarding(
    command: OnboardingDraft,
    context: PlatformIdentity,
    database: DatabaseDependency,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> OnboardingView:
    """Save a draft and pending admin. Does not issue credentials or send invitations."""
    return await TenantOnboardingService(database.session_factory).create(
        context, command, idempotency_key
    )


@router.get("/tenants/{tenant_id}/onboarding")
async def get_onboarding(
    tenant_id: UUID, context: PlatformIdentity, database: DatabaseDependency
) -> OnboardingView:
    return await TenantOnboardingService(database.session_factory).get(context, tenant_id)


@router.patch("/tenants/{tenant_id}/onboarding")
async def update_onboarding(
    tenant_id: UUID,
    command: UpdateOnboardingDraft,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> OnboardingView:
    """Update pending setup with a revision guard; replay of the last update is safe."""
    return await TenantOnboardingService(database.session_factory).update(
        context, tenant_id, command
    )
