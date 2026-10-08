"""Support capabilities remain distinct from tenant and platform roles."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app.api.domain import router as domain_router
from app.api.knowledge import router as knowledge_router
from app.api.postcall import router as postcall_router
from app.api.telephony import router as telephony_router
from app.api.tenancy import get_context
from app.api.tenancy import router as tenancy_router
from app.auth.context import ActorType, RequestContext
from app.auth.policies import SUPPORT_PERMISSIONS, Permission, require_permission
from app.errors import AuthorizationError
from app.platform.support import SUPPORT_ROUTES, EnterSupportCommand, parse_support_locator


def test_every_tenant_route_has_an_explicit_support_decision() -> None:
    def uses_tenant_context(dependant: object) -> bool:
        dependencies = getattr(dependant, "dependencies", ())
        return any(item.call == get_context or uses_tenant_context(item) for item in dependencies)

    routes = {
        (method, route.path)
        for router in (
            tenancy_router,
            domain_router,
            knowledge_router,
            postcall_router,
            telephony_router,
        )
        for route in router.routes
        if isinstance(route, APIRoute) and uses_tenant_context(route.dependant)
        for method in route.methods or ()
    }
    assert routes - set(SUPPORT_ROUTES) == {("GET", "/api/v1/users/me")}


def support_context(*capabilities: Permission) -> RequestContext:
    return RequestContext(
        request_id=uuid4(),
        tenant_id=uuid4(),
        actor_id=None,
        actor_type=ActorType.PLATFORM_SUPPORT,
        platform_user_id=uuid4(),
        platform_session_id=uuid4(),
        support_session_id=uuid4(),
        support_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        capabilities=frozenset(permission.value for permission in capabilities),
    )


def test_support_context_keeps_real_actor_and_requires_explicit_write_capability() -> None:
    context = support_context(Permission.TENANT_READ, Permission.PATIENT_READ)
    assert context.actor_id is None and context.role is None
    assert context.platform_user_id is not None and context.support_session_id is not None
    require_permission(context, Permission.PATIENT_READ)
    with pytest.raises(AuthorizationError):
        require_permission(context, Permission.PATIENT_WRITE)
    with pytest.raises(AuthorizationError):
        require_permission(context, Permission.TENANT_UPDATE)
    elevated = support_context(Permission.TENANT_UPDATE)
    require_permission(elevated, Permission.TENANT_UPDATE)
    with pytest.raises(AuthorizationError):
        require_permission(elevated, Permission.PATIENT_WRITE)


def test_reason_alone_grants_full_access_and_a_narrower_list_is_still_honoured() -> None:
    base = {"tenant_id": str(uuid4()), "reason": "Customer asked for help"}
    full = EnterSupportCommand.model_validate(base)
    # Full tenant authority is bounded separately from the platform login.
    assert set(full.granted) == SUPPORT_PERMISSIONS and full.duration_minutes == 30
    assert {
        Permission.PATIENT_WRITE,
        Permission.INTEGRATION_MANAGE,
        Permission.TENANT_ACTIVATE,
        Permission.USER_INVITE,
        Permission.USER_MANAGE,
    } <= (SUPPORT_PERMISSIONS)
    narrow = EnterSupportCommand.model_validate({**base, "capabilities": ["patient:read"]})
    assert narrow.granted == [Permission.PATIENT_READ]
    invalid_commands: tuple[dict[str, object], ...] = (
        {**base, "reason": "  "},
        {"tenant_id": base["tenant_id"]},
        {**base, "capabilities": ["patient:write", "patient:write"]},
        {**base, "capabilities": ["user:delete"]},
        {**base, "capabilities": []},
    )
    for invalid in invalid_commands:
        with pytest.raises(ValidationError):
            EnterSupportCommand.model_validate(invalid)


@pytest.mark.parametrize("token", ["", "fd1.bad", "fds1.bad.bad.bad", "fds1." + "x" * 160])
def test_invalid_support_token_locator(token: str) -> None:
    from app.errors import AuthenticationError

    with pytest.raises(AuthenticationError):
        parse_support_locator(token)
