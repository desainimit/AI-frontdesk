from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response

from app.api.tenancy import Context, DatabaseDependency
from app.auth.context import ActorType
from app.auth.policies import Permission, require_permission
from app.business.schemas import (
    BusinessListPage,
    BusinessListQuery,
    BusinessRecordView,
    DirectoryView,
    DirectoryWrite,
    DirectoryWriteResult,
)
from app.business.service import BusinessDirectoryService
from app.configuration.resources import (
    ConfigurationResourceService,
    ConfigurationResourceView,
    CreateConfigurationResource,
    UpdateConfigurationResource,
)
from app.configuration.schemas import (
    ConfigurationDiff,
    ConfigurationHead,
    ConfigurationRevision,
    ConfigurationScopesResult,
    ConfigurationScopeView,
    ConfigurationSectionView,
    ConfigurationTest,
    ConfigurationTestView,
    ConfigurationValidation,
    ConfigurationVersionView,
    ConfigurationView,
    EffectiveConfigurationView,
    PublicationSummary,
    SaveConfiguration,
    SaveConfigurationScopes,
    SaveConfigurationSection,
)
from app.configuration.sections import ConfigurationSection
from app.configuration.service import ConfigurationService
from app.core.security.encryption import SecretCipher
from app.platform.support import SUPPORT_ROUTES
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

router = APIRouter(prefix="/api/v2", tags=["workspace configuration"])


@router.get("/business/records/{identity}")
async def business_record(
    identity: UUID, context: Context, database: DatabaseDependency
) -> BusinessRecordView:
    return await BusinessDirectoryService(database.session_factory).record(context, identity)


@router.get("/business/mutations/{identity}")
async def business_receipt(
    identity: UUID, context: Context, database: DatabaseDependency
) -> DirectoryWriteResult:
    return await BusinessDirectoryService(database.session_factory).receipt(context, identity)


@router.get("/business/records")
async def business_records(
    request: Request,
    context: Context,
    database: DatabaseDependency,
    query: Annotated[BusinessListQuery, Query()],
) -> BusinessListPage:
    settings = request.app.state.settings
    cipher = SecretCipher(settings.secret_encryption_keys, settings.secret_encryption_active_key)
    return await BusinessDirectoryService(database.session_factory).list_records(
        context, query, cipher
    )


@router.get("/business/directory")
async def business_directory(context: Context, database: DatabaseDependency) -> DirectoryView:
    return await BusinessDirectoryService(database.session_factory).get_directory(context)


@router.get("/business/catalog")
async def business_catalog(context: Context, database: DatabaseDependency) -> dict[str, object]:
    return await BusinessDirectoryService(database.session_factory).directory_catalog(context)


@router.put("/business/directory")
async def stage_business_record(
    command: DirectoryWrite, context: Context, database: DatabaseDependency
) -> DirectoryWriteResult:
    return await BusinessDirectoryService(database.session_factory).write(context, command)


@router.get("/capabilities")
async def capabilities(context: Context) -> CapabilitiesView:
    return CapabilitiesView(
        tenant_id=context.tenant_id,
        actor_id=context.platform_user_id
        if context.actor_type == ActorType.PLATFORM_SUPPORT
        else context.actor_id,
        role_id=context.role_id,
        role_name=context.role_name or "Super Admin",
        authorization_revision=context.authorization_revision,
        permissions=[
            permission for permission in Permission if permission.value in context.capabilities
        ],
    )


@router.get("/catalog")
async def catalog(context: Context, database: DatabaseDependency) -> dict[str, object]:
    require_permission(context, Permission.TENANT_READ)
    return await ConfigurationService(database.session_factory).catalog(context)


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


@router.get("/configuration")
async def configuration(context: Context, database: DatabaseDependency) -> ConfigurationView:
    return await ConfigurationService(database.session_factory).get(context)


@router.get("/configuration/head")
async def configuration_head(context: Context, database: DatabaseDependency) -> ConfigurationHead:
    return await ConfigurationService(database.session_factory).head(context)


@router.get("/configuration/scopes/{kind}/{identity}")
async def configuration_scope(
    kind: Literal["location", "agent", "channel"],
    identity: UUID,
    context: Context,
    database: DatabaseDependency,
) -> ConfigurationScopeView:
    return await ConfigurationService(database.session_factory).get_scope(context, kind, identity)


@router.put("/configuration")
async def save_configuration(
    command: SaveConfiguration, context: Context, database: DatabaseDependency
) -> ConfigurationView:
    return await ConfigurationService(database.session_factory).save(context, command)


@router.get("/configuration/sections/{section}")
async def configuration_section(
    section: ConfigurationSection, context: Context, database: DatabaseDependency
) -> ConfigurationSectionView:
    return await ConfigurationService(database.session_factory).get_section(context, section)


@router.put("/configuration/sections/{section}")
async def save_configuration_section(
    section: ConfigurationSection,
    command: SaveConfigurationSection,
    context: Context,
    database: DatabaseDependency,
) -> ConfigurationSectionView:
    return await ConfigurationService(database.session_factory).save_section(
        context, section, command
    )


@router.get("/configuration/sections/{section}/diff")
async def configuration_section_diff(
    section: ConfigurationSection,
    context: Context,
    database: DatabaseDependency,
    revision: Annotated[int, Query(ge=1)],
) -> ConfigurationDiff:
    return await ConfigurationService(database.session_factory).section_diff(
        context, section, revision
    )


@router.post("/configuration/sections/{section}/publish")
async def publish_configuration_section(
    section: ConfigurationSection,
    command: ConfigurationRevision,
    context: Context,
    database: DatabaseDependency,
) -> PublicationSummary:
    result = await ConfigurationService(database.session_factory).publish(
        context, command.revision, section=section
    )
    return PublicationSummary.model_validate(result, from_attributes=True)


@router.post("/configuration/resources", status_code=201)
async def create_configuration_resource(
    command: CreateConfigurationResource, context: Context, database: DatabaseDependency
) -> ConfigurationResourceView:
    return await ConfigurationResourceService(database.session_factory).create(context, command)


@router.put("/configuration/scopes")
async def save_configuration_scopes(
    command: SaveConfigurationScopes, context: Context, database: DatabaseDependency
) -> ConfigurationScopesResult:
    return await ConfigurationService(database.session_factory).save_scopes(context, command)


@router.put("/configuration/resources/{identity}")
async def update_configuration_resource(
    identity: UUID,
    command: UpdateConfigurationResource,
    context: Context,
    database: DatabaseDependency,
) -> ConfigurationResourceView:
    return await ConfigurationResourceService(database.session_factory).update(
        context, identity, command
    )


@router.post("/configuration/validate")
async def validate_configuration(
    command: ConfigurationRevision, context: Context, database: DatabaseDependency
) -> ConfigurationValidation:
    return await ConfigurationService(database.session_factory).validate(context, command.revision)


@router.post("/configuration/publish")
async def publish_configuration(
    command: ConfigurationRevision, context: Context, database: DatabaseDependency
) -> ConfigurationVersionView:
    return await ConfigurationService(database.session_factory).publish(context, command.revision)


@router.get("/configuration/diff")
async def configuration_diff(
    context: Context,
    database: DatabaseDependency,
    revision: Annotated[int, Query(ge=1)],
) -> ConfigurationDiff:
    return await ConfigurationService(database.session_factory).diff(context, revision)


@router.post("/configuration/test")
async def test_configuration(
    command: ConfigurationTest, context: Context, database: DatabaseDependency
) -> ConfigurationTestView:
    return await ConfigurationService(database.session_factory).test(context, command)


@router.get("/configuration/effective")
async def effective_configuration(
    context: Context,
    database: DatabaseDependency,
    location_id: UUID | None = None,
    agent_id: UUID | None = None,
    channel_id: UUID | None = None,
) -> EffectiveConfigurationView:
    return await ConfigurationService(database.session_factory).effective(
        context, location_id=location_id, agent_id=agent_id, channel_id=channel_id
    )


@router.get("/configuration/history")
async def configuration_history(
    context: Context, database: DatabaseDependency
) -> list[ConfigurationVersionView]:
    return await ConfigurationService(database.session_factory).history(context)


@router.post("/configuration/rollback/{version}")
async def rollback_configuration(
    version: int, command: ConfigurationRevision, context: Context, database: DatabaseDependency
) -> ConfigurationView:
    return await ConfigurationService(database.session_factory).rollback(
        context, version, command.revision
    )


# Explicit registration means newly added unclassified endpoints remain denied.
for method, path, permission in (
    ("GET", "/business/records/{identity}", Permission.BUSINESS_READ),
    ("GET", "/business/mutations/{identity}", Permission.BUSINESS_WRITE),
    ("GET", "/business/records", Permission.BUSINESS_READ),
    ("GET", "/business/directory", Permission.BUSINESS_READ),
    ("GET", "/business/catalog", Permission.BUSINESS_READ),
    ("PUT", "/business/directory", Permission.BUSINESS_WRITE),
    ("GET", "/capabilities", None),
    ("GET", "/onboarding", Permission.TENANT_READ),
    ("PUT", "/onboarding", Permission.TENANT_UPDATE),
    ("GET", "/catalog", Permission.TENANT_READ),
    ("GET", "/permissions", Permission.ROLE_READ),
    ("GET", "/roles", Permission.ROLE_READ),
    ("POST", "/roles", Permission.ROLE_CREATE),
    ("PUT", "/roles/{identity}", Permission.ROLE_UPDATE),
    ("DELETE", "/roles/{identity}", Permission.ROLE_DELETE),
    ("PUT", "/users/{identity}/role", Permission.ROLE_ASSIGN),
    ("GET", "/users/assignable-roles", None),
    ("GET", "/configuration", Permission.CONFIGURATION_READ),
    ("GET", "/configuration/head", Permission.TENANT_READ),
    ("PUT", "/configuration", Permission.CONFIGURATION_UPDATE),
    ("GET", "/configuration/sections/{section}", Permission.TENANT_READ),
    ("PUT", "/configuration/sections/{section}", Permission.TENANT_READ),
    ("GET", "/configuration/sections/{section}/diff", Permission.TENANT_READ),
    ("POST", "/configuration/sections/{section}/publish", Permission.TENANT_READ),
    ("POST", "/configuration/resources", Permission.TENANT_READ),
    ("PUT", "/configuration/resources/{identity}", Permission.TENANT_READ),
    ("PUT", "/configuration/scopes", Permission.TENANT_READ),
    ("GET", "/configuration/scopes/{kind}/{identity}", Permission.TENANT_READ),
    ("POST", "/configuration/validate", Permission.CONFIGURATION_READ),
    ("POST", "/configuration/publish", Permission.CONFIGURATION_PUBLISH),
    ("GET", "/configuration/diff", Permission.CONFIGURATION_READ),
    ("POST", "/configuration/test", Permission.CONFIGURATION_TEST),
    ("GET", "/configuration/effective", Permission.CONFIGURATION_READ),
    ("GET", "/configuration/history", Permission.CONFIGURATION_READ),
    ("POST", "/configuration/rollback/{version}", Permission.CONFIGURATION_ROLLBACK),
):
    SUPPORT_ROUTES[(method, f"/api/v2{path}")] = permission
