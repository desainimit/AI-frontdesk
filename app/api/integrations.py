"""Tenant provider connections, readiness and activation.

The same domain service backs two route sets: a tenant administrator acting on their
own workspace, and a platform administrator acting on one tenant named in the path.
Tenant routes never trust a tenant identifier supplied by the caller.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from app.api.platform import PlatformIdentity
from app.api.tenancy import Context, DatabaseDependency
from app.auth.context import RequestContext
from app.errors import AuthenticationError
from app.integrations.catalog import ProviderCatalogEntry, catalog_entries
from app.integrations.resolver import TenantIntegrationResolver
from app.integrations.service import (
    Actor,
    BindNumber,
    CreateIntegration,
    IntegrationService,
    IntegrationView,
    PhoneBindingView,
    PlatformActor,
    ProviderNumberView,
    RevisionCommand,
    SubmitCredentials,
    UpdateIntegration,
    VerificationView,
    VerifyCommand,
    VersionCommand,
)
from app.tenants.readiness import ReadinessView, TenantReadinessService

IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128)]


def get_resolver(request: Request, database: DatabaseDependency) -> TenantIntegrationResolver:
    resolver = getattr(request.app.state, "integration_resolver", None)
    if isinstance(resolver, TenantIntegrationResolver):
        return resolver
    return TenantIntegrationResolver(database.session_factory, request.app.state.settings)


Resolver = Annotated[TenantIntegrationResolver, Depends(get_resolver)]


def get_service(
    request: Request, database: DatabaseDependency, resolver: Resolver
) -> IntegrationService:
    return IntegrationService(
        database.session_factory,
        request.app.state.settings,
        verifier=getattr(request.app.state, "integration_verifier", None),
        resolver=resolver,
    )


def get_readiness(
    request: Request, database: DatabaseDependency, resolver: Resolver
) -> TenantReadinessService:
    return TenantReadinessService(database.session_factory, request.app.state.settings, resolver)


Service = Annotated[IntegrationService, Depends(get_service)]
Readiness = Annotated[TenantReadinessService, Depends(get_readiness)]


async def tenant_actor(request: Request, context: Context) -> None:
    request.state.integration_actor = context


async def platform_actor(request: Request, tenant_id: UUID, context: PlatformIdentity) -> None:
    request.state.integration_actor = PlatformActor(context, tenant_id)


def acting_actor(request: Request) -> Actor:
    # Set by the router-level dependency, which authenticates before any route runs.
    actor = getattr(request.state, "integration_actor", None)
    if not isinstance(actor, RequestContext | PlatformActor):
        raise AuthenticationError()
    return actor


Acting = Annotated[Actor, Depends(acting_actor)]


def _routes(
    prefix: str, tenant_prefix: str, authenticate: Callable[..., Awaitable[None]], tag: str
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=[tag], dependencies=[Depends(authenticate)])

    @router.get("/integrations")
    async def list_integrations(acting: Acting, service: Service) -> list[IntegrationView]:
        return await service.list_all(acting)

    @router.post("/integrations", status_code=201)
    async def create_integration(
        command: CreateIntegration, acting: Acting, service: Service
    ) -> IntegrationView:
        return await service.create(acting, command)

    @router.get("/integrations/{integration_id}")
    async def get_integration(
        integration_id: UUID, acting: Acting, service: Service
    ) -> IntegrationView:
        return await service.get(acting, integration_id)

    @router.patch("/integrations/{integration_id}")
    async def update_integration(
        integration_id: UUID, command: UpdateIntegration, acting: Acting, service: Service
    ) -> IntegrationView:
        """Update allowlisted, nonsecret configuration. Secrets use credential-versions."""
        return await service.update(acting, integration_id, command)

    @router.post("/integrations/{integration_id}/credential-versions", status_code=201)
    async def submit_credentials(
        integration_id: UUID,
        command: SubmitCredentials,
        acting: Acting,
        service: Service,
        idempotency_key: IdempotencyKey,
    ) -> IntegrationView:
        """Write-only. The response carries version metadata, never the submitted secret."""
        return await service.submit_credentials(acting, integration_id, command, idempotency_key)

    @router.post("/integrations/{integration_id}/verify")
    async def verify_integration(
        integration_id: UUID, command: VerifyCommand, acting: Acting, service: Service
    ) -> VerificationView:
        return await service.verify(acting, integration_id, command)

    @router.post("/integrations/{integration_id}/activate-version")
    async def activate_version(
        integration_id: UUID, command: VersionCommand, acting: Acting, service: Service
    ) -> IntegrationView:
        return await service.activate(acting, integration_id, command)

    @router.post("/integrations/{integration_id}/disconnect")
    async def disconnect_integration(
        integration_id: UUID, command: RevisionCommand, acting: Acting, service: Service
    ) -> IntegrationView:
        return await service.disconnect(acting, integration_id, command)

    @router.get("/integrations/{integration_id}/phone-numbers")
    async def provider_numbers(
        integration_id: UUID, acting: Acting, service: Service
    ) -> list[ProviderNumberView]:
        return await service.provider_numbers(acting, integration_id)

    @router.post("/integrations/{integration_id}/phone-numbers", status_code=201)
    async def bind_number(
        integration_id: UUID, command: BindNumber, acting: Acting, service: Service
    ) -> PhoneBindingView:
        return await service.bind_number(acting, integration_id, command)

    @router.get(f"{tenant_prefix}/readiness")
    async def readiness(acting: Acting, service: Readiness) -> ReadinessView:
        return await service.readiness(acting)

    @router.post(f"{tenant_prefix}/activate")
    async def activate(acting: Acting, service: Readiness) -> ReadinessView:
        """Recheck readiness and start taking calls. Fails with 409 while anything blocks."""
        return await service.set_live(acting, True)

    @router.post(f"{tenant_prefix}/deactivate")
    async def deactivate(acting: Acting, service: Readiness) -> ReadinessView:
        """Stop accepting new calls without suspending administration access."""
        return await service.set_live(acting, False)

    _ = (
        list_integrations,
        create_integration,
        get_integration,
        update_integration,
        submit_credentials,
        verify_integration,
        activate_version,
        disconnect_integration,
        provider_numbers,
        bind_number,
        readiness,
        activate,
        deactivate,
    )
    return router


router = APIRouter()


@router.get("/api/v1/integration-providers", tags=["integrations"])
async def integration_providers(request: Request, context: Context) -> list[ProviderCatalogEntry]:
    """Supported providers and their allowlisted settings. Contains no stored values."""
    return _catalog(request, context)


@router.get("/api/v1/platform/integration-providers", tags=["platform"])
async def platform_integration_providers(
    request: Request, context: PlatformIdentity
) -> list[ProviderCatalogEntry]:
    return _catalog(request, context)


def _catalog(request: Request, _authenticated: Any) -> list[ProviderCatalogEntry]:
    settings = request.app.state.settings
    return catalog_entries(settings.llm_models)


router.include_router(_routes("/api/v1", "/tenants/current", tenant_actor, "integrations"))
router.include_router(
    _routes("/api/v1/platform/tenants/{tenant_id}", "", platform_actor, "platform")
)
