"""Create a clinic workspace with everything it needs to work.

One transaction creates the workspace, its starting settings, roles, appointment types,
first location and a pending administrator. Access starts when the invitation is accepted.
"""

import hashlib
import json
import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType
from app.clinic.models import Service
from app.conversation.locales import LanguagePolicy
from app.core.database.rls import set_tenant_scope
from app.core.errors import ConflictError, NotFoundError
from app.events.idempotency import IdempotencyConflictError, InvalidIdempotencyKeyError
from app.identity.invitations import ensure_account, membership_state
from app.identity.service import cancel_challenges
from app.identity.sessions import platform_scope
from app.locations.models import Location
from app.locations.schemas import CreateLocation
from app.platform.models import PlatformAuditEvent
from app.platform.onboarding_models import TenantOnboarding
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.settings.defaults import DEFAULT_LOCATION_NAME, DEFAULT_SERVICES, DEFAULT_TIMEZONE
from app.settings.models import TenantSettings
from app.settings.store import seed_settings
from app.tenants.models import Tenant
from app.users.models import User


def initial_language_policy() -> LanguagePolicy:
    return LanguagePolicy(default_language="en", enabled_languages=["en"])


class PendingAdministrator(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class OnboardingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    administrator: PendingAdministrator
    location: CreateLocation
    language_policy: LanguagePolicy = Field(default_factory=initial_language_policy)


class OnboardingDefaults(BaseModel):
    location_name: str
    timezone: str
    language_policy: LanguagePolicy


def onboarding_defaults() -> OnboardingDefaults:
    return OnboardingDefaults(
        location_name=DEFAULT_LOCATION_NAME,
        timezone=DEFAULT_TIMEZONE,
        language_policy=initial_language_policy(),
    )


class UpdateOnboardingDraft(OnboardingDraft):
    revision: int = Field(ge=1, strict=True)


class OnboardingView(BaseModel):
    id: UUID
    tenant_id: UUID
    admin_id: UUID
    location_id: UUID
    revision: int
    status: Literal["draft", "invited"]
    administrator_status: Literal["pending", "active", "deactivated"]
    invitation_status: Literal[
        "not_sent", "queued", "sent", "delivery_failed", "expired", "accepted"
    ]
    service_state: Literal["setup", "live"]
    draft: OnboardingDraft


async def _view(session: AsyncSession, row: TenantOnboarding) -> OnboardingView:
    """Derive member and invitation state from evidence; requires the tenant scope."""
    admin = await session.get(User, row.admin_id)
    tenant = await session.get(Tenant, row.tenant_id)
    if admin is None or tenant is None:
        raise NotFoundError()
    administrator_status, invitation_status = await membership_state(session, admin)
    return OnboardingView.model_validate(
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "admin_id": row.admin_id,
            "location_id": row.location_id,
            "revision": row.revision,
            "status": row.status,
            "administrator_status": administrator_status,
            "invitation_status": invitation_status,
            "service_state": tenant.service_state,
            "draft": row.draft,
        }
    )


def _apply_languages(settings: TenantSettings, policy: LanguagePolicy) -> None:
    receptionist = {**settings.document["receptionist"], "languages": policy.model_dump()}
    settings.document = {**settings.document, "receptionist": receptionist}


def _audit(session: AsyncSession, context: PlatformContext, tenant_id: UUID, action: str) -> None:
    session.add(
        PlatformAuditEvent(
            user_id=context.user_id,
            request_id=context.request_id,
            tenant_id=tenant_id,
            resource_id=tenant_id,
            action=action,
        )
    )


class TenantOnboardingService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self, context: PlatformContext, command: OnboardingDraft, key: str
    ) -> OnboardingView:
        require_platform_permission(context, PlatformPermission.TENANT_CREATE)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise InvalidIdempotencyKeyError()
        key_digest = hashlib.sha256(key.encode()).hexdigest()
        draft = command.model_dump(mode="json")
        request_digest = hashlib.sha256(
            json.dumps(draft, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        lock = int.from_bytes(
            hashlib.sha256(f"onboarding:{context.user_id}:{key_digest}".encode()).digest()[:8],
            "big",
            signed=True,
        )
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            await session.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
            existing = await session.scalar(
                select(TenantOnboarding).where(
                    TenantOnboarding.creator_id == context.user_id,
                    TenantOnboarding.key_digest == key_digest,
                )
            )
            if existing is not None:
                if existing.request_digest != request_digest:
                    raise IdempotencyConflictError()
                # Replay returns current safe state, including edits since creation.
                await set_tenant_scope(session, existing.tenant_id)
                return await _view(session, existing)
            tenant_id, admin_id, location_id = uuid4(), uuid4(), uuid4()
            await set_tenant_scope(session, tenant_id)
            # Managing the clinic works at once; calls stay off until it goes live.
            tenant = Tenant(id=tenant_id, name=command.name, service_state="setup")
            session.add(tenant)
            await session.flush()
            from app.rbac.service import seed_roles

            roles = await seed_roles(session, tenant_id)
            settings = await seed_settings(session, tenant, command.location.timezone)
            _apply_languages(settings, command.language_policy)
            session.add_all(
                [
                    Service(tenant_id=tenant_id, name=name, duration_minutes=minutes)
                    for name, minutes in DEFAULT_SERVICES
                ]
            )
            account = await ensure_account(
                session, command.administrator.email, command.administrator.display_name
            )
            session.add_all(
                [
                    User(
                        id=admin_id,
                        tenant_id=tenant_id,
                        account_id=account.id,
                        email=command.administrator.email,
                        display_name=command.administrator.display_name,
                        role_id=roles["owner"],
                        is_active=False,
                    ),
                    Location(id=location_id, tenant_id=tenant_id, **command.location.model_dump()),
                ]
            )
            await session.flush()
            row = TenantOnboarding(
                tenant_id=tenant_id,
                admin_id=admin_id,
                location_id=location_id,
                creator_id=context.user_id,
                key_digest=key_digest,
                request_digest=request_digest,
                draft=draft,
            )
            session.add(row)
            _audit(session, context, tenant_id, "tenant_onboarding_created")
            for action, resource_type, resource_id in (
                ("tenant_created", "tenant", tenant_id),
                ("user_created", "user", admin_id),
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
            return await _view(session, row)

    async def get(self, context: PlatformContext, tenant_id: UUID) -> OnboardingView:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await session.scalar(
                select(TenantOnboarding).where(TenantOnboarding.tenant_id == tenant_id)
            )
            if row is None:
                raise NotFoundError()
            await set_tenant_scope(session, tenant_id)
            _audit(session, context, tenant_id, "tenant_onboarding_viewed")
            return await _view(session, row)

    async def update(
        self, context: PlatformContext, tenant_id: UUID, command: UpdateOnboardingDraft
    ) -> OnboardingView:
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        draft = command.model_dump(mode="json", exclude={"revision"})
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            await set_tenant_scope(session, tenant_id)
            row = await session.scalar(
                select(TenantOnboarding)
                .where(TenantOnboarding.tenant_id == tenant_id)
                .with_for_update()
            )
            if row is None:
                raise NotFoundError()
            if row.revision != command.revision:
                if row.revision == command.revision + 1 and row.draft == draft:
                    # Lost PATCH response; no duplicate update or audit.
                    return await _view(session, row)
                raise ConflictError()
            admin = await session.scalar(
                select(User).where(User.id == row.admin_id).with_for_update()
            )
            tenant = await session.get(Tenant, tenant_id)
            location = await session.get(Location, row.location_id)
            if admin is None or tenant is None or location is None:
                raise ConflictError()
            # Once the administrator has accepted, the workspace belongs to the tenant.
            if (await membership_state(session, admin))[1] == "accepted":
                raise ConflictError()
            settings = await session.scalar(
                select(TenantSettings)
                .where(TenantSettings.tenant_id == tenant_id)
                .with_for_update()
            )
            if settings is None:
                raise ConflictError()
            clinic = {
                **settings.document["clinic"],
                "name": command.name,
                "timezone": command.location.timezone,
            }
            settings.document = {**settings.document, "clinic": clinic}
            _apply_languages(settings, command.language_policy)
            tenant.name = command.name
            if admin.email != command.administrator.email:
                # A link mailed to the old address must not open the new membership.
                await cancel_challenges(session, purpose="invitation", user_id=admin.id)
                account = await ensure_account(
                    session, command.administrator.email, command.administrator.display_name
                )
                admin.account_id = account.id
                row.status = "draft"
            admin.email = command.administrator.email
            admin.display_name = command.administrator.display_name
            location.name = command.location.name
            location.timezone = command.location.timezone
            row.draft = draft
            row.revision += 1
            _audit(session, context, tenant_id, "tenant_onboarding_updated")
            await session.flush()
            return await _view(session, row)
