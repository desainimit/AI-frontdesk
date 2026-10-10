"""Creating a clinic workspace in one step, and looking after it afterwards.

One transaction saves the workspace, its settings from the platform's defaults, the
starting roles and appointment types, the first location, the owner's pending
membership, the owner's invitation and (when asked) a phone number. Nothing needs to be
opened or published before the clinic works.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType
from app.calls.models import PhoneNumber
from app.clinic.models import Service
from app.core.database.rls import set_tenant_scope
from app.core.errors import ApplicationError, ConflictError, NotFoundError
from app.core.settings import Settings
from app.events.idempotency import IdempotencyConflictError, InvalidIdempotencyKeyError
from app.identity.invitations import InvitationService, ensure_account, membership_state
from app.identity.service import cancel_challenges
from app.identity.sessions import platform_scope
from app.identity.tokens import digest_token
from app.integrations.service import PlatformActor
from app.integrations.verifiers import ProviderNumber
from app.locations.models import Location
from app.platform.defaults import load_defaults
from app.platform.models import PlatformAuditEvent, WorkspaceCreation
from app.platform.phones import PlatformPhoneService
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.rbac.service import seed_roles
from app.settings.defaults import DEFAULT_LOCATION_NAME, DEFAULT_TIMEZONE
from app.settings.models import TenantSettings
from app.tenants.models import Tenant
from app.tenants.readiness import NotReadyError, TenantReadinessService
from app.users.models import User

_EMAIL = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254, pattern=_EMAIL)
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def lowercase(cls, value: str) -> str:
        return value.lower()


class NewWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    timezone: str = DEFAULT_TIMEZONE
    address: str = Field(default="", max_length=1000)
    # The clinic's own contact number, shown to callers who ask. Not the AI line.
    phone: str = Field(default="", max_length=20, pattern=r"^(|\+[1-9][0-9]{7,14})$")
    owner: Owner
    # A number on the platform's phone account for the AI receptionist to answer.
    phone_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")
    send_invitation: bool = True
    # Start taking calls straight away, when everything needed is in place.
    go_live: bool = False

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Select an IANA time zone") from None
        return value


class OwnerView(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    status: Literal["pending", "active", "deactivated"]
    invitation: Literal["not_sent", "queued", "sent", "delivery_failed", "expired", "accepted"]


class WorkspaceView(BaseModel):
    id: UUID
    name: str
    is_active: bool
    service_state: Literal["setup", "live"]
    created_at: datetime
    timezone: str
    owner: OwnerView | None
    # Numbers the AI receptionist answers for this clinic.
    phone_numbers: list[str]


class Warning(BaseModel):
    """An optional part of creation that could not be done; the clinic itself exists."""

    step: Literal["invitation", "phone_number", "go_live"]
    message: str


class CreatedWorkspace(BaseModel):
    workspace: WorkspaceView
    warnings: list[Warning]


class OwnerNotChangeableError(ConflictError):
    code = "owner_accepted"
    public_message = "The owner has already accepted. They change their own details now."


def _key_digest(key: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise InvalidIdempotencyKeyError()
    return hashlib.sha256(key.encode()).hexdigest()


class WorkspaceService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions, self._settings = sessions, settings
        self._phones = PlatformPhoneService(sessions, settings, transport)
        self._invitations = InvitationService(sessions, settings)

    async def _view(self, session: AsyncSession, tenant: Tenant) -> WorkspaceView:
        """Requires the workspace scope: memberships and numbers are clinic data."""
        record = await session.scalar(
            select(WorkspaceCreation).where(WorkspaceCreation.tenant_id == tenant.id)
        )
        owner = await session.get(User, record.admin_id) if record else None
        owner_view = None
        if owner is not None:
            status, invitation = await membership_state(session, owner)
            owner_view = OwnerView.model_validate(
                {
                    "user_id": owner.id,
                    "email": owner.email,
                    "display_name": owner.display_name,
                    "status": status,
                    "invitation": invitation,
                }
            )
        document = await session.scalar(
            select(TenantSettings.document).where(TenantSettings.tenant_id == tenant.id)
        )
        numbers = await session.scalars(
            select(PhoneNumber.number)
            .where(PhoneNumber.tenant_id == tenant.id, PhoneNumber.is_active.is_(True))
            .order_by(PhoneNumber.number)
        )
        return WorkspaceView(
            id=tenant.id,
            name=tenant.name,
            is_active=tenant.is_active,
            service_state=tenant.service_state,  # pyright: ignore[reportArgumentType]
            created_at=tenant.created_at,
            timezone=(document or {}).get("clinic", {}).get("timezone", DEFAULT_TIMEZONE),
            owner=owner_view,
            phone_numbers=list(numbers),
        )

    async def get(self, context: PlatformContext, tenant_id: UUID) -> WorkspaceView:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            tenant = await session.get(Tenant, tenant_id)
            if tenant is None:
                raise NotFoundError()
            await set_tenant_scope(session, tenant_id)
            return await self._view(session, tenant)

    async def create(
        self, context: PlatformContext, command: NewWorkspace, key: str
    ) -> CreatedWorkspace:
        require_platform_permission(context, PlatformPermission.TENANT_CREATE)
        key_digest = _key_digest(key)
        request_digest = hashlib.sha256(
            json.dumps(command.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        warnings: list[Warning] = []

        # The phone service is asked before anything is written, so the number can be
        # saved in the same transaction as the clinic.
        number: ProviderNumber | None = None
        if command.phone_number is not None and not await self._exists(context, key_digest):
            try:
                number = await self._phones.prove(command.phone_number)
            except ApplicationError as problem:
                warnings.append(Warning(step="phone_number", message=problem.public_message))

        mail_ready = self._settings.account_mail_ready
        if command.send_invitation and not mail_ready:
            warnings.append(
                Warning(
                    step="invitation",
                    message="No invitation was sent: the email server is not set up yet. "
                    "Set it up in platform settings, then send the invitation.",
                )
            )
        lock = int.from_bytes(
            hashlib.sha256(f"workspace:{context.user_id}:{key_digest}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            await session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
            existing = await session.scalar(
                select(WorkspaceCreation).where(
                    WorkspaceCreation.creator_id == context.user_id,
                    WorkspaceCreation.key_digest == key_digest,
                )
            )
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise IdempotencyConflictError()
                # A retry answers with the clinic as it is now and repeats nothing.
                tenant = await session.get(Tenant, existing.tenant_id)
                if tenant is None:
                    raise NotFoundError()
                await set_tenant_scope(session, tenant.id)
                return CreatedWorkspace(workspace=await self._view(session, tenant), warnings=[])

            defaults = await load_defaults(session)
            tenant_id, owner_id, location_id = uuid4(), uuid4(), uuid4()
            await set_tenant_scope(session, tenant_id)
            # Managing the clinic works at once; calls start when it goes live.
            tenant = Tenant(id=tenant_id, name=command.name, service_state="setup")
            session.add(tenant)
            await session.flush()
            roles = await seed_roles(session, tenant_id)
            document = defaults.settings_for(command.name, command.timezone)
            document.clinic.address, document.clinic.phone = command.address, command.phone
            session.add(
                TenantSettings(
                    tenant_id=tenant_id, version=1, document=document.model_dump(mode="json")
                )
            )
            session.add_all(
                [
                    Service(tenant_id=tenant_id, name=item.name, duration_minutes=item.minutes)
                    for item in defaults.services
                ]
            )
            account = await ensure_account(session, command.owner.email, command.owner.display_name)
            owner = User(
                id=owner_id,
                tenant_id=tenant_id,
                account_id=account.id,
                email=command.owner.email,
                display_name=command.owner.display_name,
                role_id=roles["owner"],
                # Not active until they prove they read this mailbox.
                is_active=False,
            )
            session.add_all(
                [
                    owner,
                    Location(
                        id=location_id,
                        tenant_id=tenant_id,
                        name=DEFAULT_LOCATION_NAME,
                        timezone=command.timezone,
                        address=command.address,
                        phone=command.phone,
                    ),
                ]
            )
            await session.flush()
            session.add(
                WorkspaceCreation(
                    tenant_id=tenant_id,
                    admin_id=owner_id,
                    location_id=location_id,
                    creator_id=context.user_id,
                    key_digest=key_digest,
                    request_digest=request_digest,
                )
            )
            if number is not None:
                try:
                    async with session.begin_nested():
                        await self._phones.attach(session, tenant_id, number)
                except ApplicationError as problem:
                    number = None
                    warnings.append(Warning(step="phone_number", message=problem.public_message))
            if command.send_invitation and mail_ready:
                await self._invitations.queue_invitation(
                    session, owner, digest_token(f"workspace-invite:{tenant_id}:{key_digest}")
                )
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    request_id=context.request_id,
                    tenant_id=tenant_id,
                    resource_id=tenant_id,
                    action="workspace_created",
                )
            )
            for action, resource_type, resource_id in (
                ("tenant_created", "tenant", tenant_id),
                ("user_created", "user", owner_id),
                ("location_created", "location", location_id),
            ):
                session.add(
                    AuditEvent(
                        tenant_id=tenant_id,
                        actor_type=ActorType.SYSTEM,
                        platform_user_id=context.user_id,
                        request_id=context.request_id,
                        action=action,
                        resource_type=resource_type,
                        resource_id=resource_id,
                    )
                )
            await session.flush()

        # After the clinic is saved: point the number here, and go live if asked.
        if number is not None and not await self._phones.point_webhooks(number.sid):
            warnings.append(
                Warning(
                    step="phone_number",
                    message="The number is assigned, but the phone service could not be "
                    "pointed at this application automatically.",
                )
            )
        if command.go_live:
            try:
                await TenantReadinessService(self._sessions, self._settings).set_live(
                    PlatformActor(context, tenant_id), True
                )
            except NotReadyError:
                warnings.append(
                    Warning(
                        step="go_live",
                        message="The clinic is not taking calls yet: something it needs is "
                        "still missing. Open the clinic to see what.",
                    )
                )
        return CreatedWorkspace(workspace=await self.get(context, tenant_id), warnings=warnings)

    async def _exists(self, context: PlatformContext, key_digest: str) -> bool:
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            return (
                await session.scalar(
                    select(WorkspaceCreation.id).where(
                        WorkspaceCreation.creator_id == context.user_id,
                        WorkspaceCreation.key_digest == key_digest,
                    )
                )
            ) is not None

    async def invite_owner(
        self, context: PlatformContext, tenant_id: UUID, key: str
    ) -> WorkspaceView:
        """Send the owner's invitation, or send a new one that replaces the last."""
        await self._invitations.invite_initial_admin(context, tenant_id, key)
        return await self.get(context, tenant_id)

    async def change_owner(
        self, context: PlatformContext, tenant_id: UUID, command: Owner
    ) -> WorkspaceView:
        """Correct the owner's name or address while their invitation is unanswered."""
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            await set_tenant_scope(session, tenant_id)
            record = await session.scalar(
                select(WorkspaceCreation).where(WorkspaceCreation.tenant_id == tenant_id)
            )
            tenant = await session.get(Tenant, tenant_id)
            if record is None or tenant is None:
                raise NotFoundError()
            owner = await session.scalar(
                select(User).where(User.id == record.admin_id).with_for_update()
            )
            if owner is None:
                raise NotFoundError()
            if owner.accepted_at is not None:
                raise OwnerNotChangeableError()
            if owner.email != command.email:
                # A link mailed to the old address must not open the new membership.
                await cancel_challenges(session, purpose="invitation", user_id=owner.id)
                account = await ensure_account(session, command.email, command.display_name)
                owner.account_id = account.id
            owner.email, owner.display_name = command.email, command.display_name
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    request_id=context.request_id,
                    tenant_id=tenant_id,
                    resource_id=owner.id,
                    action="workspace_owner_changed",
                )
            )
            await session.flush()
            return await self._view(session, tenant)
