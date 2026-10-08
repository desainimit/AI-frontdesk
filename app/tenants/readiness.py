"""Server-computed readiness and the explicit switch that lets a workspace take calls.

Readiness is recomputed from current rows every time. A saved credential, a wizard step
number or an earlier check is never treated as proof, and activation re-runs the same
checks inside the transaction that flips the state.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.human_models import HumanAccount
from app.auth.models import ApiCredential
from app.auth.policies import Permission
from app.calls.models import PhoneNumber
from app.config.settings import Settings
from app.configuration.models import ConfigurationDraft
from app.errors import ApplicationError, NotFoundError
from app.integrations.catalog import Purpose
from app.integrations.models import TenantIntegration
from app.integrations.resolver import TenantIntegrationResolver
from app.integrations.service import Actor, ActorScope, enter_actor
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformPermission
from app.rbac.models import RolePermission
from app.tenants.models import Tenant
from app.users.models import User


class NotReadyError(ApplicationError):
    status_code = 409
    code = "not_ready"
    public_message = "The workspace is not ready to take calls. Review the readiness checks."


class ReadinessCheck(BaseModel):
    key: str
    status: Literal["ready", "blocking", "optional"]
    reason: str | None = None
    next_action: str | None = None


class ReadinessView(BaseModel):
    tenant_id: UUID
    service_state: Literal["setup", "live"]
    ready: bool
    checked_at: datetime
    checks: list[ReadinessCheck]


def _ready(key: str) -> ReadinessCheck:
    return ReadinessCheck(key=key, status="ready")


def _blocking(key: str, reason: str, next_action: str) -> ReadinessCheck:
    return ReadinessCheck(key=key, status="blocking", reason=reason, next_action=next_action)


class TenantReadinessService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        resolver: TenantIntegrationResolver | None = None,
    ) -> None:
        self._sessions, self._settings = sessions, settings
        self._resolver = resolver or TenantIntegrationResolver(sessions, settings)

    async def _checks(self, session: AsyncSession, tenant: Tenant) -> list[ReadinessCheck]:
        settings, tenant_id = self._settings, tenant.id
        legacy = self._resolver.legacy_allowed(tenant_id)
        integrations = {
            (row.purpose, row.slot): row
            for row in await session.scalars(
                select(TenantIntegration).where(TenantIntegration.tenant_id == tenant_id)
            )
        }

        def connection(
            key: str, purpose: Purpose, label: str, platform_configured: bool
        ) -> ReadinessCheck:
            row = integrations.get((purpose, "primary"))
            if row is not None and row.status == "verified" and row.active_version_id:
                return _ready(key)
            if row is None and legacy and platform_configured:
                return _ready(key)  # Explicitly approved to use the platform account.
            if row is None or row.status in {"not_configured", "revoked"}:
                return _blocking(key, f"No {label} connection.", f"Connect {label}.")
            if row.status == "saved_unverified":
                return _blocking(key, f"{label} credentials are not verified.", "Run Verify.")
            return _blocking(key, f"{label} credentials were rejected.", "Replace the credential.")

        checks: list[ReadinessCheck] = []
        administrator_roles = (
            select(RolePermission.role_id)
            .where(
                RolePermission.tenant_id == tenant_id,
                RolePermission.key.in_([Permission.USER_MANAGE, Permission.ROLE_UPDATE]),
                RolePermission.allowed.is_(True),
            )
            .group_by(RolePermission.role_id)
            .having(func.count(RolePermission.key) == 2)
        )
        accepted = await session.scalar(
            select(User.id)
            .join(HumanAccount, HumanAccount.user_id == User.id)
            .where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role_id.in_(administrator_roles),
            )
            .limit(1)
        )
        service_admin = await session.scalar(
            select(User.id)
            .join(ApiCredential, ApiCredential.user_id == User.id)
            .where(
                User.tenant_id == tenant_id,
                User.is_active.is_(True),
                User.role_id.in_(administrator_roles),
                ApiCredential.revoked_at.is_(None),
                ApiCredential.expires_at > datetime.now(UTC),
            )
            .limit(1)
        )
        checks.append(
            _ready("administrator")
            if accepted or service_admin
            else _blocking(
                "administrator",
                "No administrator has accepted an invitation.",
                "Send or resend the administrator invitation.",
            )
        )
        if tenant.requires_saas_activation:
            # New onboarding must never inherit legacy token-only activation behavior.
            published = await session.scalar(
                select(ConfigurationDraft.published_version).where(
                    ConfigurationDraft.tenant_id == tenant_id
                )
            )
            checks.append(
                _ready("published_configuration")
                if published is not None
                else _blocking(
                    "published_configuration",
                    "No configuration is published.",
                    "Validate and publish workspace configuration.",
                )
            )
            if not accepted:
                checks.append(
                    _blocking(
                        "accepted_administrator",
                        "The workspace administrator has not accepted membership.",
                        "Send the setup invitation and complete administrator acceptance.",
                    )
                )
        checks.append(
            _ready("public_callbacks")
            if settings.telephony_enabled and settings.telephony_public_base_url
            else _blocking(
                "public_callbacks",
                "The platform has no public telephony callback address.",
                "Ask the platform operator to enable telephony.",
            )
        )
        telephony = connection(
            "telephony_connection",
            Purpose.TELEPHONY,
            "phone service",
            bool(settings.twilio_account_sid and settings.twilio_auth_token.get_secret_value()),
        )
        checks.append(telephony)
        row = integrations.get((Purpose.TELEPHONY, "primary"))
        number = select(PhoneNumber.id).where(
            PhoneNumber.tenant_id == tenant_id, PhoneNumber.is_active.is_(True)
        )
        if row is not None:
            # A number proven against another account must not route to this connection.
            number = number.where(PhoneNumber.integration_id == row.id)
        checks.append(
            _ready("phone_number")
            if await session.scalar(number.limit(1))
            else _blocking(
                "phone_number",
                "No active phone number is bound to the phone service connection.",
                "Select a number owned by the connected account.",
            )
        )
        mock_voice = settings.telephony_mode == "echo"
        checks.append(
            connection(
                "speech_to_text",
                Purpose.STT,
                "speech-to-text",
                mock_voice or settings.primary_stt_provider == "mock" or _stt_key(settings),
            )
        )
        checks.append(
            connection(
                "text_to_speech",
                Purpose.TTS,
                "text-to-speech",
                mock_voice or settings.primary_tts_provider == "mock" or _tts_key(settings),
            )
        )
        for purpose, key in ((Purpose.STT, "speech_to_text"), (Purpose.TTS, "text_to_speech")):
            fallback = integrations.get((purpose, "fallback"))
            if fallback is not None and fallback.status not in {"verified", "revoked"}:
                checks.append(
                    _blocking(
                        f"{key}_fallback",
                        "The fallback connection is configured but not verified.",
                        "Verify the fallback or disconnect it.",
                    )
                )
        if settings.agent_enabled:
            checks.append(
                connection(
                    "agent",
                    Purpose.LLM,
                    "AI model",
                    settings.llm_provider == "mock"
                    or bool(settings.platform_llm_key.get_secret_value()),
                )
            )
        knowledge = integrations.get((Purpose.EMBEDDINGS, "primary"))
        if settings.knowledge_enabled and not (
            (knowledge is not None and knowledge.status == "verified")
            or (knowledge is None and legacy and settings.platform_llm_key.get_secret_value())
        ):
            checks.append(
                ReadinessCheck(
                    key="knowledge",
                    status="optional",
                    reason="Knowledge answers are unavailable without an embeddings connection.",
                    next_action="Connect an embeddings provider to enable knowledge.",
                )
            )
        if tenant.human_transfer_number is None:
            checks.append(
                ReadinessCheck(
                    key="transfer_destination",
                    status="optional",
                    reason="Callers cannot be transferred to staff.",
                    next_action="Set a staff transfer number.",
                )
            )
        return checks

    async def _tenant(self, session: AsyncSession, scope: ActorScope, *, lock: bool) -> Tenant:
        query = select(Tenant).where(Tenant.id == scope.tenant_id)
        tenant = await session.scalar(query.with_for_update() if lock else query)
        if tenant is None:
            raise NotFoundError()
        return tenant

    def _view(self, tenant: Tenant, checks: list[ReadinessCheck]) -> ReadinessView:
        return ReadinessView(
            tenant_id=tenant.id,
            service_state=tenant.service_state,  # pyright: ignore[reportArgumentType]
            ready=all(check.status != "blocking" for check in checks),
            checked_at=datetime.now(UTC),
            checks=checks,
        )

    async def readiness(self, actor: Actor) -> ReadinessView:
        async with self._sessions.begin() as session:
            scope = await enter_actor(
                session, actor, Permission.INTEGRATION_READ, PlatformPermission.TENANT_READ
            )
            tenant = await self._tenant(session, scope, lock=False)
            return self._view(tenant, await self._checks(session, tenant))

    async def set_live(self, actor: Actor, live: bool) -> ReadinessView:
        async with self._sessions.begin() as session:
            scope = await enter_actor(
                session, actor, Permission.TENANT_ACTIVATE, PlatformPermission.TENANT_UPDATE
            )
            tenant = await self._tenant(session, scope, lock=True)
            checks = await self._checks(session, tenant)
            view = self._view(tenant, checks)
            if live and not view.ready:
                raise NotReadyError()
            target = "live" if live else "setup"
            if tenant.service_state != target:
                tenant.service_state = target
                action = "tenant_service_activated" if live else "tenant_service_deactivated"
                session.add(
                    AuditEvent(
                        tenant_id=tenant.id,
                        actor_id=scope.user_id,
                        platform_user_id=scope.platform_user_id,
                        actor_type=scope.actor_type,
                        support_session_id=scope.support_session_id,
                        action=action,
                        resource_type="tenant",
                        resource_id=tenant.id,
                        request_id=scope.request_id,
                    )
                )
                if scope.platform_user_id is not None:
                    session.add(
                        PlatformAuditEvent(
                            user_id=scope.platform_user_id,
                            request_id=scope.request_id,
                            tenant_id=tenant.id,
                            resource_id=tenant.id,
                            action=action,
                        )
                    )
            return self._view(tenant, checks)


def _stt_key(settings: Settings) -> bool:
    key = (
        settings.deepgram_api_key
        if settings.primary_stt_provider == "deepgram"
        else settings.azure_speech_key
    )
    return bool(key.get_secret_value())


def _tts_key(settings: Settings) -> bool:
    key = (
        settings.elevenlabs_api_key
        if settings.primary_tts_provider == "elevenlabs"
        else settings.azure_speech_key
    )
    return bool(key.get_secret_value())
