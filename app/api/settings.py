"""Clinic settings, roles and what the signed-in person may do."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Query, Response
from pydantic import JsonValue

from app.api.tenancy import Context, Cursor, DatabaseDependency, Limit
from app.auth.context import ActorType
from app.auth.policies import Permission, require_permission
from app.rbac.catalog import permission_catalog
from app.rbac.schemas import (
    AssignRole,
    CapabilitiesView,
    RoleCommand,
    RoleOption,
    RoleView,
    UpdateRole,
)
from app.rbac.service import RoleService
from app.settings.schemas import SettingsChangePage, SettingsSection, SettingsView
from app.settings.service import SettingsService
from app.tenants.getting_started import GettingStarted, GettingStartedService

router = APIRouter(prefix="/api/v1", tags=["settings"])


@router.get("/settings")
async def get_settings(context: Context, database: DatabaseDependency) -> SettingsView:
    return await SettingsService(database.session_factory).get(context)


@router.put("/settings/{section}")
async def save_settings(
    section: SettingsSection,
    value: Annotated[JsonValue, Body()],
    context: Context,
    database: DatabaseDependency,
) -> SettingsView:
    """Save one section. It takes effect at once; calls already in progress are not changed."""
    return await SettingsService(database.session_factory).save(context, section, value)


@router.get("/settings/history")
async def settings_history(
    context: Context, database: DatabaseDependency, limit: Limit = 50, cursor: Cursor = None
) -> SettingsChangePage:
    return await SettingsService(database.session_factory).history(context, limit, cursor)


@router.get("/getting-started")
async def getting_started(context: Context, database: DatabaseDependency) -> GettingStarted:
    """What a new clinic still has to do, worked out from what it has done."""
    return await GettingStartedService(database.session_factory).get(context)


@router.get("/capabilities")
async def capabilities(context: Context) -> CapabilitiesView:
    return CapabilitiesView(
        actor_id=context.platform_user_id
        if context.actor_type == ActorType.PLATFORM_SUPPORT
        else context.actor_id,
        role_id=context.role_id,
        role_name=context.role_name or "Platform administrator",
        permissions=[
            permission for permission in Permission if permission.value in context.capabilities
        ],
    )


@router.get("/permissions")
async def permissions(context: Context) -> list[dict[str, object]]:
    require_permission(context, Permission.ROLE_READ)
    return permission_catalog()


@router.get("/roles")
async def roles(context: Context, database: DatabaseDependency) -> list[RoleView]:
    return await RoleService(database.session_factory).list(context)


@router.post("/roles", status_code=201)
async def create_role(
    command: RoleCommand, context: Context, database: DatabaseDependency
) -> RoleView:
    return await RoleService(database.session_factory).save(context, command)


@router.put("/roles/{identity}")
async def update_role(
    identity: UUID, command: UpdateRole, context: Context, database: DatabaseDependency
) -> RoleView:
    return await RoleService(database.session_factory).save(context, command, identity)


@router.delete("/roles/{identity}", status_code=204)
async def delete_role(
    identity: UUID,
    context: Context,
    database: DatabaseDependency,
    revision: Annotated[int, Query(ge=1)],
) -> Response:
    await RoleService(database.session_factory).delete(context, identity, revision)
    return Response(status_code=204)


@router.put("/users/{identity}/role", status_code=204)
async def assign_role(
    identity: UUID, command: AssignRole, context: Context, database: DatabaseDependency
) -> Response:
    await RoleService(database.session_factory).assign(context, identity, command.role_id)
    return Response(status_code=204)


@router.get("/users/assignable-roles")
async def assignable_roles(context: Context, database: DatabaseDependency) -> list[RoleOption]:
    return list(await RoleService(database.session_factory).assignable(context))
