"""Sign-in, the current session, passwords, two-step verification, invitations and team.

One email and password for everyone. The response to each sign-in step says what the
browser should show next; which workspace a session works in is kept on the server.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from app.api.dependencies import SessionToken, get_resources
from app.api.tenancy import Context, DatabaseDependency
from app.core.redis import RedisClient
from app.identity.invitations import InvitationService
from app.identity.mfa import MfaService
from app.identity.schemas import (
    AcceptInvitation,
    ChangePassword,
    DisableMfa,
    ForgotPassword,
    InvitationDetails,
    InvitationView,
    InviteMember,
    LinkToken,
    Login,
    MeView,
    MfaCode,
    MfaProof,
    MfaSetupView,
    RecoveryCodesView,
    ResetPassword,
    SelectWorkspace,
    SessionView,
    TeamMemberUpdate,
    WorkspaceOption,
)
from app.identity.service import IdentityService
from app.identity.throttle import AuthenticationUnavailableError, check_login_limit
from app.identity.tokens import digest_token

# Responses under /api/v1/auth are marked no-store by the request middleware.
router = APIRouter(prefix="/api/v1", tags=["auth"])
IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128)]


def _request_id(request: Request) -> UUID:
    return UUID(request.state.request_id)


def identity_service(request: Request, database: DatabaseDependency) -> IdentityService:
    return IdentityService(database.session_factory, request.app.state.settings)


def mfa_service(request: Request, database: DatabaseDependency) -> MfaService:
    return MfaService(database.session_factory, request.app.state.settings)


def invitation_service(request: Request, database: DatabaseDependency) -> InvitationService:
    return InvitationService(database.session_factory, request.app.state.settings)


Identity = Annotated[IdentityService, Depends(identity_service)]
Mfa = Annotated[MfaService, Depends(mfa_service)]
Invitations = Annotated[InvitationService, Depends(invitation_service)]


async def throttle(request: Request, scope: str, subject: str) -> None:
    """Bound guesses per subject and per address. Without Redis the route is refused,
    never left unthrottled."""
    if not request.app.state.rate_limits:
        return  # Only tests that substitute Redis turn this off.
    redis = get_resources(request).redis
    if not isinstance(redis, RedisClient):
        raise AuthenticationUnavailableError()
    address = request.client.host if request.client else "unknown"
    await check_login_limit(redis, subject, address, scope=scope)


def _no_content() -> Response:
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


# --- signing in


@router.post("/auth/login")
async def login(command: Login, request: Request, identity: Identity) -> SessionView:
    await throttle(request, "login", command.email)
    return await identity.login(command, _request_id(request))


@router.post("/auth/login/mfa")
async def login_mfa(
    command: MfaProof, request: Request, token: SessionToken, identity: Identity
) -> SessionView:
    """Finish a sign-in for an account with two-step verification on."""
    await throttle(request, "login-mfa", digest_token(token))
    return await identity.login_mfa(token, command, _request_id(request))


@router.post("/auth/logout", status_code=204)
async def logout(request: Request, token: SessionToken, identity: Identity) -> Response:
    await identity.logout(token, _request_id(request))
    return _no_content()


# --- the current session


@router.get("/auth/me")
async def me(request: Request, token: SessionToken, identity: Identity) -> MeView:
    return await identity.me(token, _request_id(request))


@router.get("/auth/workspaces")
async def workspaces(token: SessionToken, identity: Identity) -> list[WorkspaceOption]:
    """Workspaces the signed-in person belongs to."""
    return await identity.workspaces(token)


@router.post("/auth/workspace")
async def select_workspace(
    command: SelectWorkspace, request: Request, token: SessionToken, identity: Identity
) -> MeView:
    return await identity.select_workspace(token, command, _request_id(request))


@router.post("/auth/workspace/leave")
async def leave_workspace(request: Request, token: SessionToken, identity: Identity) -> MeView:
    return await identity.leave_workspace(token, _request_id(request))


# --- passwords


@router.post("/auth/password/change", status_code=204)
async def change_password(
    command: ChangePassword, request: Request, token: SessionToken, identity: Identity
) -> Response:
    await throttle(request, "password-change", digest_token(token))
    await identity.change_password(token, command, _request_id(request))
    return _no_content()


@router.post("/auth/password/forgot", status_code=202)
async def forgot_password(
    command: ForgotPassword, request: Request, identity: Identity
) -> dict[str, str]:
    """Always the same answer, whether or not the address has an account."""
    await throttle(request, "password-forgot", command.email)
    await identity.forgot_password(command, _request_id(request))
    return {"status": "accepted"}


@router.post("/auth/password/reset", status_code=204)
async def reset_password(command: ResetPassword, request: Request, identity: Identity) -> Response:
    await throttle(request, "password-reset", digest_token(command.token.get_secret_value()))
    await identity.reset_password(command, _request_id(request))
    return _no_content()


# --- two-step verification


@router.post("/auth/mfa/setup")
async def mfa_setup(request: Request, token: SessionToken, mfa: Mfa) -> MfaSetupView:
    """A new secret, as a QR image and as text for manual entry."""
    return await mfa.setup(token, _request_id(request))


@router.post("/auth/mfa/enable")
async def mfa_enable(
    command: MfaCode, request: Request, token: SessionToken, mfa: Mfa
) -> RecoveryCodesView:
    await throttle(request, "mfa", digest_token(token))
    return await mfa.enable(token, command, _request_id(request))


@router.post("/auth/mfa/recovery-codes")
async def mfa_recovery_codes(
    command: MfaCode, request: Request, token: SessionToken, mfa: Mfa
) -> RecoveryCodesView:
    await throttle(request, "mfa", digest_token(token))
    return await mfa.recovery_codes(token, command, _request_id(request))


@router.post("/auth/mfa/disable", status_code=204)
async def mfa_disable(
    command: DisableMfa, request: Request, token: SessionToken, mfa: Mfa
) -> Response:
    await throttle(request, "mfa", digest_token(token))
    await mfa.disable(token, command, _request_id(request))
    return _no_content()


# --- invitations


@router.post("/auth/invitations/details")
async def invitation_details(
    command: LinkToken, request: Request, invitations: Invitations
) -> InvitationDetails:
    await throttle(request, "invitation", digest_token(command.token.get_secret_value()))
    return await invitations.details(command)


@router.post("/auth/invitations/accept")
async def accept_invitation(
    command: AcceptInvitation, request: Request, invitations: Invitations
) -> SessionView | None:
    """Activate the membership. A new account is signed in; an existing one signs in as usual."""
    await throttle(request, "invitation", digest_token(command.token.get_secret_value()))
    return await invitations.accept(command, _request_id(request))


# --- team


@router.post("/users/invitations", status_code=201)
async def invite_member(
    command: InviteMember,
    context: Context,
    invitations: Invitations,
    idempotency_key: IdempotencyKey,
) -> InvitationView:
    return await invitations.invite_member(context, command, idempotency_key)


@router.post("/users/{user_id}/invitations", status_code=201)
async def resend_invitation(
    user_id: UUID, context: Context, invitations: Invitations, idempotency_key: IdempotencyKey
) -> InvitationView:
    return await invitations.invite_member(context, user_id, idempotency_key)


@router.patch("/users/{user_id}", status_code=204)
async def update_member(
    user_id: UUID, command: TeamMemberUpdate, context: Context, invitations: Invitations
) -> Response:
    await invitations.manage_member(context, user_id, command)
    return _no_content()


@router.post("/users/{user_id}/revoke-sessions", status_code=204)
async def revoke_member_sessions(
    user_id: UUID, context: Context, invitations: Invitations
) -> Response:
    await invitations.manage_member(context, user_id, None)
    return _no_content()
