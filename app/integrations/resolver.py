"""Resolve a tenant's own provider connection for calls and background jobs.

Callers pass a tenant they have already established (a verified call, an authorized
request or a claimed job). Nothing here mutates process configuration, and a tenant
without a usable connection gets an error rather than another tenant's or the
platform's key. Platform keys are used only for explicitly allowlisted legacy tenants.
"""

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.database.rls import set_tenant_scope, verify_runtime_role
from app.errors import ApplicationError
from app.integrations.models import CredentialVersion, TenantIntegration
from app.security.encryption import SecretCipher

MAX_CACHE_ENTRIES = 2048


class IntegrationUnavailableError(ApplicationError):
    status_code = 503
    code = "integration_unavailable"
    public_message = "A required provider connection is not available for this workspace."


class IntegrationsDisabledError(ApplicationError):
    status_code = 503
    code = "integrations_unavailable"
    public_message = "Tenant provider connections are not enabled."


def encryption_context(
    tenant_id: UUID, integration_id: UUID, provider: str, purpose: str, version: int
) -> str:
    return f"integration:{tenant_id}:{integration_id}:{provider}:{purpose}:{version}"


@dataclass(frozen=True)
class ResolvedIntegration:
    tenant_id: UUID
    integration_id: UUID
    version_id: UUID
    provider: str
    purpose: str
    slot: str
    account_identifier: str | None
    config: Mapping[str, Any]
    secrets: Mapping[str, SecretStr] = field(repr=False)

    @property
    def scope(self) -> str:
        """Isolation key for per-connection state such as circuit breakers."""
        return f"{self.tenant_id}:{self.integration_id}:{self.version_id}"

    def secret(self, name: str) -> SecretStr:
        return self.secrets[name]


RoleCheck = Callable[[AsyncSession], Awaitable[None]]


class TenantIntegrationResolver:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        role_check: RoleCheck = verify_runtime_role,
    ) -> None:
        self._sessions, self._settings, self._role_check = sessions, settings, role_check
        self._cipher: SecretCipher | None = None
        self._cache: dict[tuple[UUID, str, str], tuple[float, ResolvedIntegration | None]] = {}

    @property
    def enabled(self) -> bool:
        return self._settings.tenant_integrations_enabled

    def legacy_allowed(self, tenant_id: UUID) -> bool:
        """Whether this tenant may use the platform's provider accounts from settings."""
        return not self.enabled or tenant_id in self._settings.legacy_tenants

    def invalidate(self, tenant_id: UUID) -> None:
        for key in [key for key in self._cache if key[0] == tenant_id]:
            del self._cache[key]

    def _decrypt(
        self, integration: TenantIntegration, version: CredentialVersion
    ) -> ResolvedIntegration:
        if version.ciphertext is None:
            raise IntegrationUnavailableError()
        if self._cipher is None:
            self._cipher = SecretCipher(
                self._settings.secret_encryption_keys, self._settings.secret_encryption_active_key
            )
        try:
            payload = json.loads(
                self._cipher.decrypt(
                    version.key_id,
                    version.ciphertext,
                    encryption_context(
                        integration.tenant_id,
                        integration.id,
                        integration.provider,
                        integration.purpose,
                        version.version,
                    ),
                )
            )
        except (ValueError, InvalidTag):
            # Missing key version or tampered/misplaced ciphertext: fail closed.
            raise IntegrationUnavailableError() from None
        return ResolvedIntegration(
            tenant_id=integration.tenant_id,
            integration_id=integration.id,
            version_id=version.id,
            provider=integration.provider,
            purpose=integration.purpose,
            slot=integration.slot,
            account_identifier=integration.account_identifier,
            config=dict(integration.config),
            secrets={name: SecretStr(value) for name, value in payload.items()},
        )

    async def _load_active(
        self, session: AsyncSession, tenant_id: UUID, purpose: str, slot: str
    ) -> ResolvedIntegration | None:
        row = (
            await session.execute(
                select(TenantIntegration, CredentialVersion)
                .join(
                    CredentialVersion,
                    CredentialVersion.id == TenantIntegration.active_version_id,
                )
                .where(
                    TenantIntegration.tenant_id == tenant_id,
                    TenantIntegration.purpose == purpose,
                    TenantIntegration.slot == slot,
                    TenantIntegration.status == "verified",
                    CredentialVersion.tenant_id == tenant_id,
                    CredentialVersion.state == "active",
                )
            )
        ).first()
        return self._decrypt(row[0], row[1]) if row else None

    async def active(
        self, tenant_id: UUID, purpose: str, slot: str = "primary", *, fresh: bool = False
    ) -> ResolvedIntegration | None:
        """The verified active connection, or None when the tenant has not configured one."""
        if not self.enabled:
            return None
        key = (tenant_id, purpose, slot)
        cached = self._cache.get(key)
        if cached is not None and not fresh and cached[0] > monotonic():
            return cached[1]
        async with self._sessions.begin() as session:
            await self._role_check(session)
            await set_tenant_scope(session, tenant_id)
            resolved = await self._load_active(session, tenant_id, purpose, slot)
        ttl = self._settings.integration_cache_seconds
        if ttl > 0:
            if len(self._cache) >= MAX_CACHE_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (monotonic() + ttl, resolved)
        return resolved

    async def require(
        self, tenant_id: UUID, purpose: str, slot: str = "primary"
    ) -> ResolvedIntegration:
        resolved = await self.active(tenant_id, purpose, slot)
        if resolved is None:
            raise IntegrationUnavailableError()
        return resolved

    async def for_call(
        self, tenant_id: UUID, integration_id: UUID, version_id: UUID
    ) -> list[ResolvedIntegration]:
        """Credentials acceptable for a call pinned to one connection.

        The connection's current version comes first (providers sign with their current
        secret), then the pinned version while it is still retained. A disconnected
        connection yields nothing, so its in-flight calls stop authenticating.
        """
        async with self._sessions.begin() as session:
            await self._role_check(session)
            await set_tenant_scope(session, tenant_id)
            integration = await session.scalar(
                select(TenantIntegration).where(
                    TenantIntegration.tenant_id == tenant_id,
                    TenantIntegration.id == integration_id,
                    TenantIntegration.status == "verified",
                )
            )
            if integration is None:
                return []
            versions = (
                await session.scalars(
                    select(CredentialVersion).where(
                        CredentialVersion.tenant_id == tenant_id,
                        CredentialVersion.integration_id == integration_id,
                        CredentialVersion.id.in_(
                            [item for item in (integration.active_version_id, version_id) if item]
                        ),
                        CredentialVersion.state.in_(("active", "retired")),
                        CredentialVersion.ciphertext.is_not(None),
                    )
                )
            ).all()
            ordered = sorted(versions, key=lambda row: row.id != integration.active_version_id)
            return [self._decrypt(integration, version) for version in ordered]
