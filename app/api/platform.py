from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies import get_resources
from app.api.tenancy import DatabaseDependency, Limit, Offset, Search
from app.auth.human import HumanAuthenticationService
from app.auth.human_schemas import InvitationView
from app.core.errors import AuthenticationError
from app.core.redis import RedisClient
from app.core.security.rate_limit import enforce_rate_limit
from app.platform.mfa import (
    EnrollmentView,
    MfaCode,
    PlatformMfaService,
    RecoveryCodes,
    RecoveryIssue,
    RecoveryReplace,
    RecoveryStatus,
)
from app.platform.onboarding import (
    OnboardingDefaults,
    OnboardingDraft,
    OnboardingView,
    TenantOnboardingService,
    UpdateOnboardingDraft,
    onboarding_defaults,
)
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.schemas import LoginCommand, PlatformUserView, SessionView
from app.platform.service import PlatformAuthenticationService
from app.platform.support import EnterSupportCommand, EnterSupportView, SupportAccessService
from app.platform.tenants import (
    CreateTenantCommand,
    PlatformTenantService,
    TenantCreatedView,
    TenantStatusCommand,
)
from app.platform.throttle import AuthenticationUnavailableError, check_login_limit
from app.tenants.schemas import TenantView

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])

PLATFORM_REQUESTS_PER_MINUTE = 120
platform_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="PlatformBearer",
    description=(
        "Platform Super Admin Bearer token obtained from "
        "/api/v1/platform/auth/login: fps1.<session_id>.<secret>"
    ),
)


async def login_limit(request: Request, command: LoginCommand) -> None:
    redis = get_resources(request).redis
    if not isinstance(redis, RedisClient):
        raise AuthenticationUnavailableError()
    await check_login_limit(
        redis, command.email, request.client.host if request.client else "unknown"
    )


async def get_platform_context(
    request: Request,
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(platform_bearer)],
    database: DatabaseDependency,
) -> PlatformContext:
    if credential is None:
        raise AuthenticationError()
    context = await PlatformAuthenticationService(database.session_factory).authenticate(
        credential.credentials, UUID(request.state.request_id)
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


@router.post("/auth/login", dependencies=[Depends(login_limit)])
async def login(
    command: LoginCommand, request: Request, database: DatabaseDependency
) -> SessionView:
    return await PlatformAuthenticationService(
        database.session_factory, request.app.state.settings
    ).login(
        command,
        UUID(request.state.request_id),
        request.app.state.settings.platform_session_minutes,
    )


@router.post("/auth/mfa/enroll")
async def enroll_mfa(
    context: PlatformIdentity, request: Request, database: DatabaseDependency
) -> EnrollmentView:
    return await PlatformMfaService(database.session_factory, request.app.state.settings).begin(
        context
    )


@router.post("/auth/mfa/confirm", status_code=204)
async def confirm_mfa(
    command: MfaCode, context: PlatformIdentity, request: Request, database: DatabaseDependency
) -> Response:
    await PlatformMfaService(database.session_factory, request.app.state.settings).confirm(
        context, command
    )
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/auth/mfa/confirm-recovery")
async def confirm_mfa_with_recovery(
    command: MfaCode,
    context: PlatformIdentity,
    request: Request,
    database: DatabaseDependency,
) -> RecoveryCodes:
    return await PlatformMfaService(database.session_factory, request.app.state.settings).confirm(
        context, command
    )


@router.get("/auth/mfa/recovery")
async def recovery_status(
    context: PlatformIdentity, request: Request, database: DatabaseDependency
) -> RecoveryStatus:
    return await PlatformMfaService(
        database.session_factory, request.app.state.settings
    ).recovery_status(context)


@router.post("/auth/mfa/recovery")
async def recovery_issue(
    command: RecoveryIssue,
    context: PlatformIdentity,
    request: Request,
    database: DatabaseDependency,
) -> RecoveryCodes:
    return await PlatformMfaService(
        database.session_factory, request.app.state.settings
    ).recovery_issue(context, command)


@router.post("/auth/mfa/replace", status_code=204)
async def replace_authenticator(
    command: RecoveryReplace,
    context: PlatformIdentity,
    request: Request,
    database: DatabaseDependency,
) -> Response:
    await PlatformMfaService(database.session_factory, request.app.state.settings).replace_factor(
        context, command
    )
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/auth/logout", status_code=204)
async def logout(context: PlatformIdentity, database: DatabaseDependency) -> Response:
    await PlatformAuthenticationService(database.session_factory).logout(context)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/me")
async def me(context: PlatformIdentity, database: DatabaseDependency) -> PlatformUserView:
    return await PlatformAuthenticationService(database.session_factory).me(context)


@router.post("/support-sessions", status_code=201)
async def enter_support(
    command: EnterSupportCommand, context: PlatformIdentity, database: DatabaseDependency
) -> EnterSupportView:
    return await SupportAccessService(database.session_factory).enter(context, command)


@router.post("/support-sessions/{tenant_id}/{session_id}/revoke", status_code=204)
async def revoke_support(
    tenant_id: UUID, session_id: UUID, context: PlatformIdentity, database: DatabaseDependency
) -> Response:
    await SupportAccessService(database.session_factory).revoke(context, tenant_id, session_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/tenants")
async def tenants(
    context: PlatformIdentity,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    q: Search = None,
    is_active: bool | None = None,
    service_state: Literal["setup", "live"] | None = None,
) -> list[TenantView]:
    return await PlatformTenantService(database.session_factory).list(
        context, limit, offset, q=q, is_active=is_active, service_state=service_state
    )


@router.post("/tenants", status_code=201)
async def create_tenant(
    command: CreateTenantCommand, context: PlatformIdentity, database: DatabaseDependency
) -> TenantCreatedView:
    return await PlatformTenantService(database.session_factory).create(context, command)


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
    return await HumanAuthenticationService(
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
