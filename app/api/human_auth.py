"""Tenant password login, invitation acceptance, OTP recovery and team management."""

import json
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from app.api.dependencies import get_resources
from app.api.tenancy import Context, DatabaseDependency
from app.auth.human import HumanAuthenticationService
from app.auth.human_schemas import (
    AcceptInvitation,
    EmailIdentity,
    HumanLogin,
    HumanSessionView,
    InvitationDetails,
    InvitationToken,
    InvitationView,
    InviteMember,
    ResetPassword,
    TeamMemberUpdate,
)
from app.cache.redis import RedisClient
from app.platform.throttle import AuthenticationUnavailableError, check_login_limit

router = APIRouter(prefix="/api/v1", tags=["human-auth"])
NO_STORE = {"Cache-Control": "no-store"}
IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128)]


def service(request: Request, database: DatabaseDependency) -> HumanAuthenticationService:
    return HumanAuthenticationService(database.session_factory, request.app.state.settings)


HumanAuth = Annotated[HumanAuthenticationService, Depends(service)]


async def human_auth_limit(request: Request) -> None:
    """Throttle every identity, existing or not, before any password or OTP work."""
    redis = get_resources(request).redis
    if not isinstance(redis, RedisClient):
        raise AuthenticationUnavailableError()
    # The body is never logged. Starlette caches it for the route's own validation.
    try:
        body: object = json.loads(await request.body())
    except (ValueError, UnicodeError):
        return  # The route returns the standard safe validation response.
    if not isinstance(body, dict):
        return
    fields = cast(dict[str, object], body)
    account = f"{fields.get('tenant_id', '')}:{str(fields.get('email', '')).strip().lower()}"
    if request.url.path.endswith(("/accept", "/details")):
        account = str(fields.get("token", ""))
    address = request.client.host if request.client else "unknown"
    await check_login_limit(redis, account[:512], address, scope="human-auth")


Throttled = [Depends(human_auth_limit)]


@router.post("/tenant-invitations/details", dependencies=Throttled)
async def invitation_details(
    command: InvitationToken, response: Response, auth: HumanAuth
) -> InvitationDetails:
    response.headers.update(NO_STORE)
    return await auth.invitation_details(command)


@router.post("/tenant-auth/login", dependencies=Throttled)
async def login(
    command: HumanLogin, request: Request, response: Response, auth: HumanAuth
) -> HumanSessionView:
    response.headers.update(NO_STORE)
    return await auth.login(command, UUID(request.state.request_id))


@router.post("/tenant-invitations/accept", status_code=204, dependencies=Throttled)
async def accept(command: AcceptInvitation, request: Request, auth: HumanAuth) -> Response:
    await auth.accept(command, UUID(request.state.request_id))
    return Response(status_code=204, headers=NO_STORE)


@router.post("/tenant-auth/forgot-password", status_code=202, dependencies=Throttled)
async def forgot(
    command: EmailIdentity, request: Request, response: Response, auth: HumanAuth
) -> dict[str, str]:
    response.headers.update(NO_STORE)
    await auth.request_reset(command, UUID(request.state.request_id))
    # Identical for known and unknown accounts.
    return {"message": "If this account can recover its password, an email will be sent."}


@router.post("/tenant-auth/reset-password", status_code=204, dependencies=Throttled)
async def reset(command: ResetPassword, request: Request, auth: HumanAuth) -> Response:
    await auth.reset(command, UUID(request.state.request_id))
    return Response(status_code=204, headers=NO_STORE)


@router.post("/tenant-auth/logout", status_code=204)
async def logout(context: Context, auth: HumanAuth) -> Response:
    await auth.logout(context)
    return Response(status_code=204, headers=NO_STORE)


@router.post("/users/invitations", status_code=201)
async def invite_member(
    command: InviteMember, context: Context, auth: HumanAuth, idempotency_key: IdempotencyKey
) -> InvitationView:
    """Create a pending member and email a single-use invitation. No token is returned."""
    return await auth.invite_member(context, command, idempotency_key)


@router.post("/users/{user_id}/invitations", status_code=201)
async def resend_invitation(
    user_id: UUID, context: Context, auth: HumanAuth, idempotency_key: IdempotencyKey
) -> InvitationView:
    """Replace a pending member's invitation; the superseded link stops working."""
    return await auth.invite_member(context, user_id, idempotency_key)


@router.patch("/users/{user_id}", status_code=204)
async def update_member(
    user_id: UUID, command: TeamMemberUpdate, context: Context, auth: HumanAuth
) -> Response:
    await auth.manage_member(context, user_id, command)
    return Response(status_code=204, headers=NO_STORE)


@router.post("/users/{user_id}/revoke-sessions", status_code=204)
async def revoke_member_sessions(user_id: UUID, context: Context, auth: HumanAuth) -> Response:
    await auth.manage_member(context, user_id, None)
    return Response(status_code=204, headers=NO_STORE)
