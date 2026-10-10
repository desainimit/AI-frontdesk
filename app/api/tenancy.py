from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from app.api.dependencies import SessionToken, get_resources
from app.audit.queries import AuditQueryService
from app.audit.schemas import AuditView
from app.auth.context import ActorType, RequestContext
from app.core.database.pages import Page, decode_cursor, page
from app.core.database.session import Database
from app.core.database.unit_of_work import UnitOfWork
from app.core.security.rate_limit import enforce_rate_limit
from app.identity.sessions import authenticate_workspace
from app.platform.support import record_support_action
from app.tenants.schemas import TenantView
from app.tenants.service import TenantService
from app.users.schemas import UserView
from app.users.service import UserService

router = APIRouter(prefix="/api/v1", tags=["tenancy"])

TENANT_REQUESTS_PER_MINUTE = 240


def get_database(request: Request) -> Database:
    database = get_resources(request).database
    if not isinstance(database, Database):
        raise RuntimeError("Database resource does not support tenant operations")
    return database


async def get_context(
    request: Request,
    response: Response,
    token: SessionToken,
    database: Annotated[Database, Depends(get_database)],
) -> RequestContext:
    """Who is acting, in the workspace their session is working in."""
    context = await authenticate_workspace(
        database.session_factory, token, UUID(request.state.request_id)
    )
    await enforce_rate_limit(
        get_resources(request).redis,
        enabled=request.app.state.rate_limits,
        scope="tenant_api",
        identity=f"{context.tenant_id}:{context.support_session_id or context.actor_id}",
        limit=TENANT_REQUESTS_PER_MINUTE,
    )
    if context.actor_type == ActorType.PLATFORM_SUPPORT:
        request.state.context = context
        route = getattr(request.scope.get("route"), "path", "unmatched")
        await record_support_action(database.session_factory, context, request.method, route)
    response.headers["Cache-Control"] = "no-store"
    request.state.context = context
    return context


Context = Annotated[RequestContext, Depends(get_context)]
DatabaseDependency = Annotated[Database, Depends(get_database)]
Limit = Annotated[int, Query(ge=1, le=100)]
# Every list answers {items, next_cursor}; send next_cursor back to get the next page.
Cursor = Annotated[
    str | None, Query(max_length=200, description="next_cursor of the previous page")
]
# List filters. Every filter is optional and applied in the database, so a page of
# results is a page of matches rather than a page that the client then narrows.
Search = Annotated[str | None, Query(max_length=100, description="Case-insensitive contains")]
Since = Annotated[datetime | None, Query(description="Inclusive lower bound (ISO 8601)")]
Until = Annotated[datetime | None, Query(description="Inclusive upper bound (ISO 8601)")]


@router.get("/tenants/current")
async def current_tenant(
    request: Request, context: Context, database: DatabaseDependency
) -> TenantView:
    return await TenantService(UnitOfWork(database.session_factory)).current(context)


@router.get("/users/me")
async def current_user(context: Context, database: DatabaseDependency) -> UserView:
    return await UserService(UnitOfWork(database.session_factory)).me(context)


@router.get("/users")
async def list_users(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    q: Search = None,
    role_id: UUID | None = None,
    is_active: bool | None = None,
) -> Page[UserView]:
    offset = decode_cursor(cursor)
    rows = await UserService(UnitOfWork(database.session_factory)).list(
        context, limit + 1, offset, q=q, role_id=role_id, is_active=is_active
    )
    return page(rows, limit, offset)


@router.get("/audit-events")
async def list_audit_events(
    context: Context,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    action: Annotated[str | None, Query(max_length=80, pattern=r"^[a-z_]+$")] = None,
    actor_type: ActorType | None = None,
    resource_type: Annotated[str | None, Query(max_length=80, pattern=r"^[a-z_]+$")] = None,
    created_from: Since = None,
    created_to: Until = None,
) -> Page[AuditView]:
    offset = decode_cursor(cursor)
    rows = await AuditQueryService(UnitOfWork(database.session_factory)).list(
        context,
        limit + 1,
        offset,
        action=action,
        actor_type=actor_type,
        resource_type=resource_type,
        created_from=created_from,
        created_to=created_to,
    )
    return page(rows, limit, offset)
