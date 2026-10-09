from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.auth import router as auth_router
from app.api.domain import router as domain_router
from app.api.knowledge import router as knowledge_router
from app.api.postcall import router as postcall_router
from app.api.telephony import router as telephony_router
from app.api.tenancy import get_context
from app.api.tenancy import router as tenancy_router
from app.auth.context import ActorType, RequestContext, Role
from app.auth.policies import Permission, require_permission, require_tenant
from app.core.errors import AuthorizationError
from app.core.settings import Settings
from app.main import create_app
from app.platform.support import SUPPORT_ROUTES
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


def support_context(*capabilities: Permission) -> RequestContext:
    return RequestContext(
        request_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=None,
        actor_type=ActorType.PLATFORM_SUPPORT,
        platform_user_id=uuid4(),
        session_id=uuid4(),
        support_session_id=uuid4(),
        support_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        capabilities=frozenset(permission.value for permission in capabilities),
    )


def test_visiting_administrator_keeps_their_own_identity_and_needs_each_permission() -> None:
    context = support_context(Permission.TENANT_READ, Permission.PATIENT_READ)
    # Never a workspace member: no member id and no role.
    assert context.actor_id is None and context.role is None
    assert context.platform_user_id is not None and context.support_session_id is not None
    require_permission(context, Permission.PATIENT_READ)
    with pytest.raises(AuthorizationError):
        require_permission(context, Permission.PATIENT_WRITE)
    # The visit must name the account, its session and an end time.
    with pytest.raises(ValueError, match="Support context"):
        RequestContext(
            uuid4(),
            uuid4(),
            None,
            ActorType.PLATFORM_SUPPORT,
            platform_user_id=uuid4(),
            support_session_id=uuid4(),
            capabilities=frozenset({Permission.TENANT_READ.value}),
        )


def test_every_workspace_route_has_an_explicit_decision_for_visiting_administrators() -> None:
    def uses_workspace_context(dependant: object) -> bool:
        dependencies = getattr(dependant, "dependencies", ())
        return any(
            item.call == get_context or uses_workspace_context(item) for item in dependencies
        )

    routes = {
        (method, route.path)
        for router in (
            tenancy_router,
            auth_router,
            domain_router,
            knowledge_router,
            postcall_router,
            telephony_router,
        )
        for route in router.routes
        if isinstance(route, APIRoute) and uses_workspace_context(route.dependant)
        for method in route.methods or ()
    }
    # A new route is refused to visiting administrators until it is listed.
    assert routes - set(SUPPORT_ROUTES) == {("GET", "/api/v1/users/me")}


def test_missing_and_malformed_credentials_never_reach_the_database(settings: Settings) -> None:
    # The database URL in these settings points nowhere; a connection attempt would fail.
    app = create_app(settings, redis_factory=lambda _: FakeDependency(), rate_limits=False)
    old_formats = (
        f"fd1.{uuid4()}.{uuid4()}.{'a' * 43}",  # API token
        f"fps1.{uuid4()}.{'a' * 43}",  # platform session
        f"fhu1.{uuid4()}.{uuid4()}.{'a' * 43}",  # workspace session
    )
    with TestClient(app) as client:
        for path in ("/api/v1/users/me", "/api/v1/platform/tenants", "/api/v1/auth/me"):
            for headers in (
                {},
                {"Authorization": "Bearer malformed"},
                {"Authorization": "Basic secret"},
                *({"Authorization": f"Bearer {token}"} for token in old_formats),
            ):
                response = client.get(path, headers=headers)
                assert response.status_code == 401, (path, headers)
                assert response.headers["www-authenticate"] == "Bearer"
                assert "secret" not in response.text
