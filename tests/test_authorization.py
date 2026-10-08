from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth.context import ActorType, RequestContext, Role
from app.auth.policies import Permission, require_permission, require_tenant
from app.auth.tokens import digest_token, issue_token, parse_locator
from app.config.settings import Settings
from app.errors import AuthenticationError, AuthorizationError
from app.main import create_app
from tests.fakes import FakeDependency


def test_context_is_immutable_and_requires_user_identity() -> None:
    context = RequestContext(uuid4(), uuid4(), uuid4(), ActorType.USER, Role.ADMIN)
    with pytest.raises(FrozenInstanceError):
        context.tenant_id = uuid4()  # pyright: ignore[reportAttributeAccessIssue]
    with pytest.raises(ValueError, match="requires"):
        RequestContext(uuid4(), uuid4(), None, ActorType.USER)


def test_permissions_and_tenant_claims_fail_closed() -> None:
    context = RequestContext(
        uuid4(),
        uuid4(),
        uuid4(),
        ActorType.USER,
        Role.MEMBER,
        role_id=uuid4(),
        capabilities=frozenset({Permission.TENANT_READ.value}),
    )
    require_permission(context, Permission.TENANT_READ)
    with pytest.raises(AuthorizationError):
        require_permission(context, Permission.USER_READ)
    with pytest.raises(AuthorizationError):
        require_tenant(context, uuid4())
    with pytest.raises(AuthorizationError):
        require_permission(
            RequestContext(uuid4(), uuid4(), None, ActorType.SYSTEM), Permission.USER_READ
        )
    with pytest.raises(AuthorizationError):
        require_permission(
            RequestContext(uuid4(), uuid4(), uuid4(), ActorType.USER, Role.ADMIN),
            Permission.TENANT_READ,
        )


def test_random_credentials_are_masked_and_parsed_as_untrusted_locators() -> None:
    tenant_id = uuid4()
    credential_id, token, digest = issue_token(tenant_id)
    parsed = parse_locator(token.get_secret_value())
    assert parsed.tenant_id == tenant_id and parsed.credential_id == credential_id
    assert digest == digest_token(token.get_secret_value())
    assert token.get_secret_value() not in repr(token)
    assert issue_token(tenant_id)[2] != digest


@pytest.mark.parametrize("token", ["", "invalid", "fd1.bad.bad.secret", "x" * 200])
def test_invalid_token_format(token: str) -> None:
    with pytest.raises(AuthenticationError):
        parse_locator(token)


def test_missing_and_malformed_auth_never_connect_to_database(settings: Settings) -> None:
    app = create_app(settings, redis_factory=lambda _: FakeDependency())
    with TestClient(app) as client:
        for headers in (
            {},
            {"Authorization": "Bearer malformed"},
            {"Authorization": "Basic secret"},
        ):
            response = client.get("/api/v1/users/me", headers=headers)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "secret" not in response.text
