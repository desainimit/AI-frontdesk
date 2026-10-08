from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.dependencies import get_resources
from app.audit.queries import AuditQueryService
from app.audit.schemas import AuditView
from app.auth.context import ActorType, RequestContext, Role
from app.auth.policies import require_tenant
from app.auth.service import AuthenticationService
from app.calls.verification import VerificationPolicy
from app.config.settings import Settings
from app.conversation.locales import LanguagePolicy
from app.database.session import Database
from app.database.unit_of_work import UnitOfWork
from app.errors import AuthenticationError, AuthorizationError
from app.platform.support import SupportAccessService, SupportSessionView
from app.security.rate_limit import enforce_rate_limit
from app.tenants.schemas import TenantName, TenantView, TransferDestination
from app.tenants.service import TenantService
from app.users.schemas import UserView
from app.users.service import UserService

router = APIRouter(prefix="/api/v1", tags=["tenancy"])
bearer = HTTPBearer(
    auto_error=False,
    scheme_name="TenantBearer",
    description="Tenant fd1 token or scoped Super Admin fds1 support token",
)


def get_database(request: Request) -> Database:
    database = get_resources(request).database
    if not isinstance(database, Database):
        raise RuntimeError("Database resource does not support tenant operations")
    return database


async def get_context(
    request: Request,
    response: Response,
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    database: Annotated[Database, Depends(get_database)],
    tenant_header: Annotated[UUID | None, Header(alias="X-Tenant-ID")] = None,
) -> RequestContext:
    if credential is None or credential.scheme.lower() != "bearer":
        raise AuthenticationError()
    if credential.credentials.startswith("fds1."):
        context = await SupportAccessService(database.session_factory).authenticate(
            credential.credentials, UUID(request.state.request_id)
        )
    else:
        context = await AuthenticationService(database.session_factory).authenticate(
            credential.credentials, UUID(request.state.request_id)
        )
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(
        get_resources(request).redis,
        enabled=request.app.state.rate_limits,
        scope="tenant_api",
        identity=f"{context.tenant_id}:{context.support_session_id or context.actor_id}",
        limit=settings.tenant_api_requests_per_minute,
    )
    if context.actor_type == ActorType.PLATFORM_SUPPORT:
        request.state.context = context
        route = getattr(request.scope.get("route"), "path", "unmatched")
        await SupportAccessService(database.session_factory).action(
            context, request.method, route, tenant_header
        )
    if tenant_header is not None:
        require_tenant(context, tenant_header)
    response.headers["Cache-Control"] = "no-store"
    request.state.context = context
    return context


Context = Annotated[RequestContext, Depends(get_context)]
DatabaseDependency = Annotated[Database, Depends(get_database)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]
# List filters. Every filter is optional and applied in the database, so a page of
# results is a page of matches rather than a page that the client then narrows.
Search = Annotated[str | None, Query(max_length=100, description="Case-insensitive contains")]
Since = Annotated[datetime | None, Query(description="Inclusive lower bound (ISO 8601)")]
Until = Annotated[datetime | None, Query(description="Inclusive upper bound (ISO 8601)")]


@router.get("/support/me")
async def current_support(context: Context) -> SupportSessionView:
    if context.actor_type != ActorType.PLATFORM_SUPPORT:
        raise AuthorizationError()
    if (
        context.support_session_id is None
        or context.platform_user_id is None
        or context.support_expires_at is None
    ):
        raise AuthenticationError()
    return SupportSessionView(
        id=context.support_session_id,
        tenant_id=context.tenant_id,
        platform_user_id=context.platform_user_id,
        capabilities=sorted(context.capabilities),
        expires_at=context.support_expires_at,
    )


@router.post("/support/exit", status_code=204)
async def exit_support(context: Context, database: DatabaseDependency) -> Response:
    if context.actor_type != ActorType.PLATFORM_SUPPORT:
        raise AuthorizationError()
    await SupportAccessService(database.session_factory).exit(context)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/tenants/current")
async def current_tenant(
    request: Request, context: Context, database: DatabaseDependency
) -> TenantView:
    settings: Settings = request.app.state.settings
    return await TenantService(UnitOfWork(database.session_factory)).current(
        context, settings.conversation_default_language
    )


@router.patch("/tenants/current")
async def rename_tenant(
    command: TenantName, context: Context, database: DatabaseDependency
) -> TenantView:
    return await TenantService(UnitOfWork(database.session_factory)).rename(context, command)


@router.get("/users/me")
async def current_user(context: Context, database: DatabaseDependency) -> UserView:
    return await UserService(UnitOfWork(database.session_factory)).me(context)


@router.get("/users")
async def list_users(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    q: Search = None,
    role: Role | None = None,
    role_id: UUID | None = None,
    is_active: bool | None = None,
) -> list[UserView]:
    return await UserService(UnitOfWork(database.session_factory)).list(
        context, limit, offset, q=q, role=role, role_id=role_id, is_active=is_active
    )


@router.get("/audit-events")
async def list_audit_events(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    offset: Offset = 0,
    action: Annotated[str | None, Query(max_length=80, pattern=r"^[a-z_]+$")] = None,
    actor_type: ActorType | None = None,
    resource_type: Annotated[str | None, Query(max_length=80, pattern=r"^[a-z_]+$")] = None,
    created_from: Since = None,
    created_to: Until = None,
) -> list[AuditView]:
    return await AuditQueryService(UnitOfWork(database.session_factory)).list(
        context,
        limit,
        offset,
        action=action,
        actor_type=actor_type,
        resource_type=resource_type,
        created_from=created_from,
        created_to=created_to,
    )


@router.put("/tenants/current/languages")
async def update_tenant_languages(
    command: LanguagePolicy, context: Context, database: DatabaseDependency
) -> TenantView:
    return await TenantService(UnitOfWork(database.session_factory)).set_language_policy(
        context, command
    )


@router.put("/tenants/current/transfer")
async def update_tenant_transfer(
    command: TransferDestination, context: Context, database: DatabaseDependency
) -> TenantView:
    return await TenantService(UnitOfWork(database.session_factory)).set_transfer_destination(
        context, command
    )


@router.put("/tenants/current/verification")
async def update_tenant_verification(
    command: VerificationPolicy, context: Context, database: DatabaseDependency
) -> TenantView:
    return await TenantService(UnitOfWork(database.session_factory)).set_verification_policy(
        context, command
    )
