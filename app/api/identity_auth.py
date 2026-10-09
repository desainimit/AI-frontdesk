"""Identity admission deliberately has no tenant-business authorization dependency."""

import json
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from app.api.dependencies import get_resources
from app.api.tenancy import DatabaseDependency
from app.auth.global_recovery import GlobalRecoveryService
from app.auth.human_schemas import HumanSessionView
from app.auth.workspace_schemas import (
    GlobalEmail,
    GlobalPasswordReset,
    GlobalRecoveryView,
    IdentityCredential,
    IdentityLogin,
    IdentitySessionView,
    WorkspaceSelection,
    WorkspacesView,
)
from app.auth.workspaces import WorkspaceIdentityService
from app.core.redis import RedisClient
from app.core.security.rate_limit import enforce_rate_limit
from app.platform.throttle import AuthenticationUnavailableError, check_login_limit

router = APIRouter(prefix="/api/v2/identity", tags=["identity admission"])


def service(request: Request, database: DatabaseDependency) -> WorkspaceIdentityService:
    return WorkspaceIdentityService(database.session_factory, request.app.state.settings)


IdentityAuth = Annotated[WorkspaceIdentityService, Depends(service)]


def recovery_service(request: Request, database: DatabaseDependency) -> GlobalRecoveryService:
    return GlobalRecoveryService(database.session_factory, request.app.state.settings)


Recovery = Annotated[GlobalRecoveryService, Depends(recovery_service)]


async def password_limit(request: Request, scope: str) -> None:
    redis = get_resources(request).redis
    if not isinstance(redis, RedisClient):
        raise AuthenticationUnavailableError()
    try:
        parsed: object = json.loads(await request.body())
    except (ValueError, UnicodeError):
        return
    if not isinstance(parsed, dict):
        return
    body = cast(dict[str, object], parsed)
    email = str(body.get("email", "")).strip().lower()[:254]
    address = request.client.host if request.client else "unknown"
    await check_login_limit(redis, email, address, scope=scope)


async def login_limit(request: Request) -> None:
    await password_limit(request, "identity-login")


async def recovery_limit(request: Request) -> None:
    await password_limit(request, "identity-recovery-request")


async def reset_limit(request: Request) -> None:
    await password_limit(request, "identity-recovery-reset")


async def admission_limit(request: Request) -> None:
    # Password-proven traffic has its own bounded address limit; the service still
    # verifies the admission token and current credential revision on every request.
    await enforce_rate_limit(
        get_resources(request).redis,
        enabled=True,
        scope="identity-admission",
        identity=request.client.host if request.client else "unknown",
        limit=120,
    )


@router.post("/login", dependencies=[Depends(login_limit)])
async def login(
    command: IdentityLogin, request: Request, response: Response, auth: IdentityAuth
) -> IdentitySessionView:
    response.headers["Cache-Control"] = "no-store"
    return await auth.login(command, UUID(request.state.request_id))


@router.post("/workspaces", dependencies=[Depends(admission_limit)])
async def workspaces(
    command: IdentityCredential, response: Response, auth: IdentityAuth
) -> WorkspacesView:
    response.headers["Cache-Control"] = "no-store"
    return await auth.workspaces(command)


@router.post("/select", dependencies=[Depends(admission_limit)])
async def select_workspace(
    command: WorkspaceSelection, request: Request, response: Response, auth: IdentityAuth
) -> HumanSessionView:
    response.headers["Cache-Control"] = "no-store"
    return await auth.select(command, UUID(request.state.request_id))


@router.post("/logout", status_code=204, dependencies=[Depends(admission_limit)])
async def logout(command: IdentityCredential, auth: IdentityAuth) -> Response:
    await auth.logout(command)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.post("/password/request", status_code=202, dependencies=[Depends(recovery_limit)])
async def request_password_recovery(
    command: GlobalEmail, request: Request, response: Response, recovery: Recovery
) -> GlobalRecoveryView:
    response.headers["Cache-Control"] = "no-store"
    return await recovery.request(command, UUID(request.state.request_id))


@router.post("/password/reset", status_code=204, dependencies=[Depends(reset_limit)])
async def reset_password(
    command: GlobalPasswordReset, request: Request, recovery: Recovery
) -> Response:
    await recovery.reset(command, UUID(request.state.request_id))
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
