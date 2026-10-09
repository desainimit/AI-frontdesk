from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AuthenticationError
from app.core.resources import Resources
from app.identity.tokens import SESSION, parse_token

bearer = HTTPBearer(
    auto_error=False,
    scheme_name="Session",
    description="The session token returned by /api/v1/auth/login: fs1.<id>.<secret>",
)


def get_resources(request: Request) -> Resources:
    return cast(Resources, request.app.state.resources)


def get_session_token(
    credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if credential is None or credential.scheme.lower() != "bearer":
        raise AuthenticationError()
    # A malformed token is refused here, before any database connection is opened.
    parse_token(credential.credentials, SESSION)
    return credential.credentials


SessionToken = Annotated[str, Depends(get_session_token)]
