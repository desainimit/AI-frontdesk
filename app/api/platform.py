from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from app.api.dependencies import SessionToken, get_resources
from app.api.tenancy import Cursor, DatabaseDependency, Limit, Search
from app.core.database.pages import Page, decode_cursor, page
from app.core.security.rate_limit import enforce_rate_limit
from app.identity.schemas import OpenWorkspace, SupportVisit
from app.identity.service import IdentityService
from app.identity.sessions import authenticate_platform
from app.integrations.catalog import ProviderCatalogEntry, Purpose, catalog_entries
from app.notifications.service import MessageView, PlatformMessageLog, Status
from app.platform.connections import (
    ConnectionView,
    PlatformConnectionService,
    ProductSettings,
    SaveConnection,
    TestMail,
)
from app.platform.defaults import DefaultsView, WorkspaceDefaults, WorkspaceDefaultsService
from app.platform.health import SystemHealth, SystemHealthService
from app.platform.phones import AssignNumber, PlatformNumber, PlatformPhoneService
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.tenants import PlatformTenantService, TenantStatusCommand
from app.platform.workspaces import (
    CreatedWorkspace,
    NewWorkspace,
    Owner,
    WorkspaceService,
    WorkspaceView,
)
from app.tenants.schemas import TenantView

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])

PLATFORM_REQUESTS_PER_MINUTE = 120


class ChecklistItem(BaseModel):
    # email, telephony, stt, tts, llm, app_address or call_address
    key: str
    done: bool
    needed_for: Literal["invitations", "calls"]


async def get_platform_context(
    request: Request, token: SessionToken, database: DatabaseDependency
) -> PlatformContext:
    """The signed-in platform administrator; anyone else is refused."""
    context = await authenticate_platform(
        database.session_factory, token, UUID(request.state.request_id)
    )
    await enforce_rate_limit(
        get_resources(request).redis,
        enabled=request.app.state.rate_limits,
        scope="platform_api",
        identity=str(context.user_id),
        limit=PLATFORM_REQUESTS_PER_MINUTE,
    )
    return context


PlatformIdentity = Annotated[PlatformContext, Depends(get_platform_context)]


@router.post("/workspaces/{tenant_id}/open")
async def open_workspace(
    tenant_id: UUID,
    command: OpenWorkspace,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> SupportVisit:
    """Start working inside a workspace for a stated reason; ends after 30 minutes."""
    return await IdentityService(
        database.session_factory, request.app.state.settings
    ).open_workspace(context, tenant_id, command)


@router.get("/tenants")
async def tenants(
    context: PlatformIdentity,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    q: Search = None,
    is_active: bool | None = None,
    service_state: Literal["setup", "live"] | None = None,
) -> Page[TenantView]:
    offset = decode_cursor(cursor)
    rows = await PlatformTenantService(database.session_factory).list(
        context, limit + 1, offset, q=q, is_active=is_active, service_state=service_state
    )
    return page(rows, limit, offset)


@router.patch("/tenants/{tenant_id}")
async def set_tenant_active(
    tenant_id: UUID,
    command: TenantStatusCommand,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> TenantView:
    return await PlatformTenantService(database.session_factory).set_active(
        context, tenant_id, command
    )


def _workspaces(request: Request, database: DatabaseDependency) -> WorkspaceService:
    return WorkspaceService(
        database.session_factory,
        request.app.state.settings,
        getattr(request.app.state, "provider_transport", None),
    )


IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128)]


@router.post("/workspaces", status_code=201)
async def create_workspace(
    command: NewWorkspace,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey,
) -> CreatedWorkspace:
    """Create a clinic that works at once: settings, roles, appointment types, owner.

    The owner's invitation, a phone number and going live are part of the same request
    when asked for. If one of those cannot be done the clinic is still created and
    `warnings` says what is left. A retry with the same key creates nothing new.
    """
    return await _workspaces(request, database).create(context, command, idempotency_key)


@router.get("/workspaces/{tenant_id}")
async def get_workspace(
    tenant_id: UUID, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> WorkspaceView:
    return await _workspaces(request, database).get(context, tenant_id)


@router.post("/workspaces/{tenant_id}/invitation")
async def invite_workspace_owner(
    tenant_id: UUID,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
    idempotency_key: IdempotencyKey,
) -> WorkspaceView:
    """Send the owner's invitation; a new key sends a fresh link and cancels the last."""
    return await _workspaces(request, database).invite_owner(context, tenant_id, idempotency_key)


@router.put("/workspaces/{tenant_id}/owner")
async def change_workspace_owner(
    tenant_id: UUID,
    command: Owner,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> WorkspaceView:
    """Correct the owner's name or email while their invitation is unanswered."""
    return await _workspaces(request, database).change_owner(context, tenant_id, command)


@router.get("/defaults")
async def workspace_defaults(
    context: PlatformIdentity, database: DatabaseDependency
) -> DefaultsView:
    """What a new clinic starts with."""
    return await WorkspaceDefaultsService(database.session_factory).get(context)


@router.put("/defaults")
async def save_workspace_defaults(
    command: WorkspaceDefaults, context: PlatformIdentity, database: DatabaseDependency
) -> DefaultsView:
    return await WorkspaceDefaultsService(database.session_factory).save(context, command)


@router.delete("/defaults")
async def reset_workspace_defaults(
    context: PlatformIdentity, database: DatabaseDependency
) -> DefaultsView:
    """Go back to the product's own defaults."""
    return await WorkspaceDefaultsService(database.session_factory).save(context, None)


def _connections(request: Request, database: DatabaseDependency) -> PlatformConnectionService:
    return PlatformConnectionService(
        database.session_factory,
        request.app.state.settings,
        getattr(request.app.state, "integration_verifier", None),
    )


@router.get("/settings")
async def product_settings(
    request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> ProductSettings:
    return await _connections(request, database).product(context)


@router.put("/settings")
async def save_product_settings(
    command: ProductSettings,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> ProductSettings:
    return await _connections(request, database).save_product(context, command)


@router.get("/connections")
async def platform_connections(
    request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> list[ConnectionView]:
    """The platform's provider accounts: email, phone, speech and AI, with their status."""
    return await _connections(request, database).list(context)


@router.get("/connections/providers")
async def platform_providers(
    request: Request, context: PlatformIdentity
) -> list[ProviderCatalogEntry]:
    require_platform_permission(context, PlatformPermission.TENANT_READ)
    return catalog_entries(request.app.state.settings.llm_models, platform=True)


@router.put("/connections/{purpose}")
async def save_platform_connection(
    purpose: Purpose,
    command: SaveConnection,
    request: Request,
    context: PlatformIdentity,
    database: DatabaseDependency,
) -> ConnectionView:
    """Save the details and check them with the provider in one step."""
    return await _connections(request, database).save(context, purpose, command)


@router.post("/connections/{purpose}/verify")
async def verify_platform_connection(
    purpose: Purpose, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> ConnectionView:
    return await _connections(request, database).verify(context, purpose)


@router.delete("/connections/{purpose}", status_code=204)
async def remove_platform_connection(
    purpose: Purpose, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> None:
    await _connections(request, database).remove(context, purpose)


@router.post("/connections/email/test", status_code=204)
async def send_test_mail(
    command: TestMail, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> None:
    await _connections(request, database).send_test_mail(context, command)


def _phones(request: Request, database: DatabaseDependency) -> PlatformPhoneService:
    return PlatformPhoneService(
        database.session_factory,
        request.app.state.settings,
        getattr(request.app.state, "provider_transport", None),
    )


@router.get("/phone-numbers")
async def platform_numbers(
    request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> list[PlatformNumber]:
    """Numbers on the platform's phone account and the clinic each one serves."""
    return await _phones(request, database).numbers(context)


@router.post("/phone-numbers", status_code=201)
async def assign_number(
    command: AssignNumber, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> PlatformNumber:
    """Give a number to a clinic and point its call webhooks at this application."""
    return await _phones(request, database).assign(context, command)


@router.post("/phone-numbers/{number}/address")
async def repoint_number(
    number: str, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> PlatformNumber:
    """Send an assigned number's calls and texts to this application again."""
    return await _phones(request, database).repoint(context, number)


@router.delete("/phone-numbers/{number}", status_code=204)
async def release_number(
    number: str, request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> None:
    await _phones(request, database).release(context, number)


@router.get("/health")
async def system_health(context: PlatformIdentity, database: DatabaseDependency) -> SystemHealth:
    """Whether the product is working: the worker, its queues, the receptionist."""
    return await SystemHealthService(database.session_factory).read(context)


@router.get("/checklist")
async def platform_checklist(
    request: Request, context: PlatformIdentity, database: DatabaseDependency
) -> list[ChecklistItem]:
    """What still has to be set up before clinics can be created and take calls."""
    connections = {
        view.purpose: view for view in await _connections(request, database).list(context)
    }
    settings = request.app.state.settings
    calls: tuple[Purpose, ...] = (Purpose.TELEPHONY, Purpose.STT, Purpose.TTS, Purpose.LLM)
    return [
        ChecklistItem(
            key="email",
            done=connections[Purpose.EMAIL].status == "verified",
            needed_for="invitations",
        ),
        ChecklistItem(
            key="app_address",
            done=bool(settings.tenant_auth_public_origin),
            needed_for="invitations",
        ),
        *(
            ChecklistItem(
                key=purpose.value,
                done=connections[purpose].status == "verified",
                needed_for="calls",
            )
            for purpose in calls
        ),
        ChecklistItem(key="call_address", done=settings.telephony_ready, needed_for="calls"),
    ]


@router.get("/messages")
async def platform_message_log(
    context: PlatformIdentity,
    database: DatabaseDependency,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Status | None = None,
) -> Page[MessageView]:
    """Messages of every clinic, newest first: what was sent, to whom (masked), and how it went."""
    offset = decode_cursor(cursor)
    rows = await PlatformMessageLog(database.session_factory).log(
        context, limit + 1, offset, status=status
    )
    return page(rows, limit, offset)
