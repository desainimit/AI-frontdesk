"""Manage a tenant's provider connections without ever returning a stored secret.

Secrets are write-only: submitted once, encrypted as an immutable version, verified
against the provider and only then promoted. A failed check never displaces the
version that is currently serving calls.
"""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.models import PhoneNumber
from app.core.database.rls import set_tenant_scope
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import ApplicationError, AuthorizationError, ConflictError, NotFoundError
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.events.idempotency import IdempotencyConflictError, InvalidIdempotencyKeyError
from app.integrations.catalog import (
    ConnectionSlot,
    InvalidIntegrationError,
    ProviderCatalogEntry,
    ProviderSpec,
    Purpose,
    catalog_entries,
    spec_for,
    validate_config,
    validate_secrets,
)
from app.integrations.models import CredentialVersion, TenantIntegration
from app.integrations.resolver import (
    TenantIntegrationResolver,
    encryption_context,
)
from app.integrations.verifiers import (
    HttpProviderVerifier,
    ProviderUnavailableError,
    ProviderVerifier,
    Verification,
)
from app.platform.models import PlatformAuditEvent
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.platform.tenants import platform_scope
from app.tenants.models import Tenant

VERIFICATION_LEASE = timedelta(seconds=60)
VERIFICATION_VALIDITY = timedelta(hours=1)


class IntegrationValidationError(ApplicationError):
    status_code = 422
    code = "invalid_integration"
    public_message = "The provider connection request is not valid."


class NumberNotOwnedError(ApplicationError):
    status_code = 422
    code = "number_not_owned"
    public_message = "The connected provider account does not own this phone number."


class ProviderCheckUnavailableError(ApplicationError):
    status_code = 503
    code = "provider_unavailable"
    public_message = "The provider could not be reached. Nothing was changed."


@dataclass(frozen=True)
class PlatformActor:
    """A platform administrator acting on one explicitly named tenant."""

    context: PlatformContext
    tenant_id: UUID


Actor = RequestContext | PlatformActor


@dataclass(frozen=True)
class ActorScope:
    tenant_id: UUID
    request_id: UUID
    user_id: UUID | None
    platform_user_id: UUID | None
    actor_type: ActorType = ActorType.SYSTEM
    support_session_id: UUID | None = None


async def enter_actor(
    session: AsyncSession,
    actor: Actor,
    permission: Permission,
    platform_permission: PlatformPermission,
) -> ActorScope:
    """Authorize inside the transaction and scope it to exactly one tenant.

    Open to the tenant's administrators and to a platform administrator, either on
    the platform routes or inside an audited support session. Callers are refused.
    """
    if isinstance(actor, PlatformActor):
        require_platform_permission(actor.context, platform_permission)
        await platform_scope(session, actor.context)
        await set_tenant_scope(session, actor.tenant_id)
        if await session.scalar(select(Tenant.id).where(Tenant.id == actor.tenant_id)) is None:
            raise NotFoundError()
        return ActorScope(actor.tenant_id, actor.context.request_id, None, actor.context.user_id)
    require_permission(actor, permission)
    if actor.actor_type not in {ActorType.USER, ActorType.PLATFORM_SUPPORT}:
        raise AuthorizationError()
    # Rechecks the tenant membership, or the support session and its platform login.
    await enter_tenant_scope(session, actor)
    return ActorScope(
        actor.tenant_id,
        actor.request_id,
        actor.actor_id,
        actor.platform_user_id,
        actor.actor_type,
        actor.support_session_id,
    )


_Scope = ActorScope


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateIntegration(_Command):
    purpose: Purpose
    provider: str = Field(min_length=1, max_length=30, pattern=r"^[a-z_]+$")
    slot: ConnectionSlot = ConnectionSlot.PRIMARY
    account_identifier: str | None = Field(default=None, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateIntegration(_Command):
    revision: int = Field(ge=1, strict=True)
    config: dict[str, Any]
    # Switching provider or account is allowed only while no credential is stored:
    # a stored secret is encrypted for, and was checked against, the current one.
    provider: str | None = Field(default=None, min_length=1, max_length=30, pattern=r"^[a-z_]+$")
    account_identifier: str | None = Field(default=None, max_length=64)


class SubmitCredentials(_Command):
    # hide_input_in_errors keeps a rejected secret out of validation output and logs.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=1, strict=True)
    secrets: dict[str, str] = Field(min_length=1, max_length=8, repr=False)


class VersionCommand(_Command):
    revision: int = Field(ge=1, strict=True)
    version_id: UUID


class VerifyCommand(VersionCommand):
    activate: bool = True


class RevisionCommand(_Command):
    revision: int = Field(ge=1, strict=True)


class BindNumber(_Command):
    number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")


class CredentialVersionView(BaseModel):
    id: UUID
    version: int
    state: Literal["staged", "verified", "active", "retired", "invalid"]
    display_suffix: str
    verifying: bool
    created_at: datetime
    validated_at: datetime | None
    error_code: str | None


class IntegrationView(BaseModel):
    id: UUID
    tenant_id: UUID
    purpose: Purpose
    slot: ConnectionSlot
    provider: str
    ownership: Literal["byo", "platform"]
    account_identifier: str | None
    config: dict[str, Any]
    status: Literal[
        "not_configured", "saved_unverified", "verifying", "verified", "invalid", "revoked"
    ]
    revision: int
    has_secret: bool
    active_version: CredentialVersionView | None
    pending_version: CredentialVersionView | None
    last_checked_at: datetime | None
    updated_at: datetime


class VerificationView(BaseModel):
    outcome: Literal["verified", "invalid", "unavailable"]
    code: str
    activated: bool
    integration: IntegrationView


class ProviderNumberView(BaseModel):
    number: str
    provider_number_sid: str
    bound: bool


class PhoneBindingView(BaseModel):
    id: UUID
    tenant_id: UUID
    integration_id: UUID
    number: str
    provider_number_sid: str
    is_active: bool
    # Callback addresses to set on the number at the provider; never changed by us.
    voice_webhook_url: str
    status_callback_url: str


def _version_view(row: CredentialVersion, now: datetime) -> CredentialVersionView:
    return CredentialVersionView(
        id=row.id,
        version=row.version,
        state=row.state,  # pyright: ignore[reportArgumentType]
        display_suffix=row.display_suffix,
        verifying=row.lease_until is not None and row.lease_until > now,
        created_at=row.created_at,
        validated_at=row.validated_at,
        error_code=row.error_code,
    )


def _view(row: TenantIntegration, versions: list[CredentialVersion]) -> IntegrationView:
    now = datetime.now(UTC)
    active = next((item for item in versions if item.id == row.active_version_id), None)
    newest = max(versions, key=lambda item: item.version, default=None)
    pending = newest if newest is not None and newest is not active else None
    if pending is not None and (pending.state == "retired" or pending.ciphertext is None):
        # Nothing is waiting once its secret has been erased, e.g. by a disconnect.
        pending = None
    verifying = any(item.lease_until is not None and item.lease_until > now for item in versions)
    return IntegrationView.model_validate(
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "purpose": row.purpose,
            "slot": row.slot,
            "provider": row.provider,
            "ownership": row.ownership,
            "account_identifier": row.account_identifier,
            "config": row.config,
            "status": "verifying" if verifying else row.status,
            "revision": row.revision,
            "has_secret": active is not None,
            "active_version": _version_view(active, now) if active else None,
            "pending_version": _version_view(pending, now) if pending else None,
            "last_checked_at": row.last_checked_at,
            "updated_at": row.updated_at,
        }
    )


class IntegrationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        verifier: ProviderVerifier | None = None,
        resolver: TenantIntegrationResolver | None = None,
    ) -> None:
        self._sessions, self._settings, self._resolver = sessions, settings, resolver
        self._verifier = verifier or HttpProviderVerifier(8)

    def _cipher(self) -> SecretCipher:
        return SecretCipher(
            self._settings.secret_encryption_keys, self._settings.secret_encryption_active_key
        )

    def catalog(self) -> list[ProviderCatalogEntry]:
        return catalog_entries(self._settings.llm_models)

    def _spec(self, purpose: str, provider: str, config: dict[str, Any]) -> dict[str, Any]:
        try:
            spec = spec_for(purpose, provider)
            validated = validate_config(spec, config)
        except InvalidIntegrationError:
            raise IntegrationValidationError() from None
        if purpose == Purpose.LLM and validated["model"] not in self._settings.llm_models:
            raise IntegrationValidationError()
        return validated

    async def _enter(self, session: AsyncSession, actor: Actor, *, write: bool) -> _Scope:
        return await enter_actor(
            session,
            actor,
            Permission.INTEGRATION_MANAGE if write else Permission.INTEGRATION_READ,
            PlatformPermission.TENANT_UPDATE if write else PlatformPermission.TENANT_READ,
        )

    def _audit(
        self, session: AsyncSession, scope: _Scope, action: str, integration_id: UUID
    ) -> None:
        # Actor, connection and outcome only: never a secret, suffix or provider response.
        session.add(
            AuditEvent(
                tenant_id=scope.tenant_id,
                actor_id=scope.user_id,
                platform_user_id=scope.platform_user_id,
                actor_type=scope.actor_type,
                support_session_id=scope.support_session_id,
                action=action,
                resource_type="integration",
                resource_id=integration_id,
                request_id=scope.request_id,
            )
        )
        if scope.platform_user_id is not None:
            session.add(
                PlatformAuditEvent(
                    user_id=scope.platform_user_id,
                    request_id=scope.request_id,
                    tenant_id=scope.tenant_id,
                    resource_id=integration_id,
                    action=action,
                )
            )

    async def _locked(
        self, session: AsyncSession, scope: _Scope, integration_id: UUID
    ) -> TenantIntegration:
        row = await session.scalar(
            select(TenantIntegration)
            .where(
                TenantIntegration.tenant_id == scope.tenant_id,
                TenantIntegration.id == integration_id,
            )
            .with_for_update()
        )
        if row is None:
            raise NotFoundError()
        return row

    async def _versions(
        self, session: AsyncSession, integration: TenantIntegration
    ) -> list[CredentialVersion]:
        return list(
            await session.scalars(
                select(CredentialVersion)
                .where(
                    CredentialVersion.tenant_id == integration.tenant_id,
                    CredentialVersion.integration_id == integration.id,
                )
                .order_by(CredentialVersion.version)
            )
        )

    async def _result(
        self, session: AsyncSession, integration: TenantIntegration
    ) -> IntegrationView:
        await session.flush()
        await session.refresh(integration)
        return _view(integration, await self._versions(session, integration))

    def _changed(self, tenant_id: UUID) -> None:
        if self._resolver is not None:
            self._resolver.invalidate(tenant_id)

    async def list_all(self, actor: Actor) -> list[IntegrationView]:
        self._cipher()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=False)
            rows = (
                await session.scalars(
                    select(TenantIntegration)
                    .where(TenantIntegration.tenant_id == scope.tenant_id)
                    .order_by(TenantIntegration.purpose, TenantIntegration.slot)
                )
            ).all()
            return [_view(row, await self._versions(session, row)) for row in rows]

    async def get(self, actor: Actor, integration_id: UUID) -> IntegrationView:
        self._cipher()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=False)
            row = await session.scalar(
                select(TenantIntegration).where(
                    TenantIntegration.tenant_id == scope.tenant_id,
                    TenantIntegration.id == integration_id,
                )
            )
            if row is None:
                raise NotFoundError()
            return _view(row, await self._versions(session, row))

    async def create(self, actor: Actor, command: CreateIntegration) -> IntegrationView:
        self._cipher()
        config = self._spec(command.purpose, command.provider, command.config)
        spec = spec_for(command.purpose, command.provider)
        account = command.account_identifier
        if command.slot not in spec.slots or (
            (account is None) != (spec.account_pattern is None)
            or (
                account is not None
                and spec.account_pattern is not None
                and not re.fullmatch(spec.account_pattern, account)
            )
        ):
            raise IntegrationValidationError()
        try:
            async with self._sessions.begin() as session:
                scope = await self._enter(session, actor, write=True)
                row = TenantIntegration(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    purpose=command.purpose,
                    slot=command.slot,
                    provider=command.provider,
                    ownership="byo",
                    account_identifier=account,
                    config=config,
                )
                session.add(row)
                await session.flush()
                self._audit(session, scope, "integration_created", row.id)
                return await self._result(session, row)
        except IntegrityError:
            # One connection per purpose and slot; replace credentials on the existing one.
            raise ConflictError() from None

    async def update(
        self, actor: Actor, integration_id: UUID, command: UpdateIntegration
    ) -> IntegrationView:
        self._cipher()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row = await self._locked(session, scope, integration_id)
            provider = command.provider or row.provider
            config = self._spec(row.purpose, provider, command.config)
            spec = spec_for(row.purpose, provider)
            account = command.account_identifier or row.account_identifier
            if spec.account_pattern is None:
                account = None
            elif account is None or not re.fullmatch(spec.account_pattern, account):
                raise IntegrationValidationError()
            if row.slot not in spec.slots:
                raise IntegrationValidationError()
            rebinding = provider != row.provider or account != row.account_identifier
            if row.revision != command.revision:
                if row.revision == command.revision + 1 and row.config == config and not rebinding:
                    return await self._result(session, row)  # Lost response; no second write.
                raise ConflictError()
            versions = await self._versions(session, row)
            if any(item.ciphertext is not None for item in versions) and (
                rebinding
                or any(row.config.get(name) != config.get(name) for name in spec.locked_fields)
            ):
                # The stored credential was checked against this endpoint; disconnect first.
                raise ConflictError()
            if rebinding:
                row.provider, row.account_identifier = provider, account
                row.status, row.last_checked_at = "not_configured", None
            row.config = config
            row.revision += 1
            self._audit(session, scope, "integration_updated", row.id)
            view = await self._result(session, row)
        self._changed(view.tenant_id)
        return view

    async def submit_credentials(
        self, actor: Actor, integration_id: UUID, command: SubmitCredentials, key: str
    ) -> IntegrationView:
        cipher = self._cipher()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise InvalidIdempotencyKeyError()
        key_digest = hashlib.sha256(
            f"integration-credential:{integration_id}:{key}".encode()
        ).hexdigest()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row = await self._locked(session, scope, integration_id)
            spec: ProviderSpec = spec_for(row.purpose, row.provider)
            try:
                values = validate_secrets(spec, command.secrets)
            except InvalidIntegrationError:
                raise IntegrationValidationError() from None
            payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
            fingerprint = cipher.fingerprint(payload, f"{scope.tenant_id}:{row.id}")
            versions = await self._versions(session, row)
            replay = next((item for item in versions if item.key_digest == key_digest), None)
            if replay is not None:
                if not hmac.compare_digest(replay.fingerprint, fingerprint):
                    raise IdempotencyConflictError()
                return _view(row, versions)
            if row.revision != command.revision or row.ownership != "byo":
                raise ConflictError()
            number = max((item.version for item in versions), default=0) + 1
            key_id, ciphertext = cipher.encrypt(
                payload,
                encryption_context(scope.tenant_id, row.id, row.provider, row.purpose, number),
            )
            for item in versions:
                # A newer submission supersedes anything not yet promoted.
                if item.state in {"staged", "verified"}:
                    item.state, item.ciphertext = "retired", None
                    item.lease_token, item.lease_until = None, None
            session.add(
                CredentialVersion(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    integration_id=row.id,
                    version=number,
                    key_id=key_id,
                    ciphertext=ciphertext,
                    display_suffix=values[spec.suffix_field][-4:],
                    fingerprint=fingerprint,
                    key_digest=key_digest,
                    created_by_user_id=scope.user_id,
                    created_by_platform_user_id=scope.platform_user_id,
                )
            )
            if row.active_version_id is None:
                row.status = "saved_unverified"
            row.revision += 1
            self._audit(session, scope, "integration_credential_staged", row.id)
            return await self._result(session, row)

    async def verify(
        self, actor: Actor, integration_id: UUID, command: VerifyCommand
    ) -> VerificationView:
        cipher = self._cipher()
        lease = uuid4()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row = await self._locked(session, scope, integration_id)
            if row.revision != command.revision:
                raise ConflictError()
            version = await session.get(CredentialVersion, command.version_id)
            now = datetime.now(UTC)
            if (
                version is None
                or version.integration_id != row.id
                or version.tenant_id != scope.tenant_id
                or version.ciphertext is None
                or version.state not in {"staged", "verified", "active"}
                or (version.lease_until is not None and version.lease_until > now)
            ):
                raise ConflictError()
            secrets: dict[str, str] = json.loads(
                cipher.decrypt(
                    version.key_id,
                    version.ciphertext,
                    encryption_context(
                        scope.tenant_id, row.id, row.provider, row.purpose, version.version
                    ),
                )
            )
            version.lease_token, version.lease_until = lease, now + VERIFICATION_LEASE
            purpose, provider = row.purpose, row.provider
            account, config = row.account_identifier, dict(row.config)
        # The provider call holds no transaction or row lock; its outcome is recorded
        # only if this exact attempt still owns the lease.
        outcome: Verification = await self._verifier.verify(
            purpose, provider, account, config, secrets
        )
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row = await self._locked(session, scope, integration_id)
            version = await session.get(CredentialVersion, command.version_id)
            if version is None or version.lease_token != lease:
                raise ConflictError()
            version.lease_token, version.lease_until = None, None
            activated = False
            if outcome.outcome == "verified" and version.ciphertext is not None:
                version.validated_at, version.error_code = datetime.now(UTC), None
                row.last_checked_at = version.validated_at
                if version.state == "staged":
                    version.state = "verified"
                if version.state == "active":
                    row.status = "verified"
                elif command.activate:
                    await self._promote(session, row, version)
                    activated = True
            elif outcome.outcome == "invalid":
                version.error_code = outcome.code[:40]
                row.last_checked_at = datetime.now(UTC)
                if version.state == "active":
                    # The provider now rejects the live credential: stop using it.
                    row.status = "invalid"
                else:
                    version.state = "invalid"
                    if row.active_version_id is None:
                        row.status = "invalid"
            row.revision += 1
            self._audit(session, scope, f"integration_verification_{outcome.outcome}", row.id)
            view = await self._result(session, row)
        self._changed(view.tenant_id)
        return VerificationView(
            outcome=outcome.outcome, code=outcome.code, activated=activated, integration=view
        )

    async def _promote(
        self, session: AsyncSession, row: TenantIntegration, version: CredentialVersion
    ) -> None:
        for item in await self._versions(session, row):
            if item.id == version.id:
                continue
            if item.state == "retired":
                # Keep only the just-replaced generation for calls already in progress.
                item.ciphertext = None
            elif item.state == "active":
                item.state = "retired"
            elif item.state in {"staged", "verified"}:
                item.state, item.ciphertext = "retired", None
        version.state = "active"
        await session.flush()
        row.active_version_id = version.id
        row.status = "verified"

    async def activate(
        self, actor: Actor, integration_id: UUID, command: VersionCommand
    ) -> IntegrationView:
        self._cipher()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row = await self._locked(session, scope, integration_id)
            version = await session.get(CredentialVersion, command.version_id)
            if version is not None and version.id == row.active_version_id:
                return await self._result(session, row)  # Already promoted; safe replay.
            if (
                row.revision != command.revision
                or version is None
                or version.integration_id != row.id
                or version.state != "verified"
                or version.ciphertext is None
                or version.validated_at is None
                # Promote on current evidence, not on a check from long ago.
                or datetime.now(UTC) - version.validated_at > VERIFICATION_VALIDITY
            ):
                raise ConflictError()
            await self._promote(session, row, version)
            row.revision += 1
            self._audit(session, scope, "integration_credential_activated", row.id)
            view = await self._result(session, row)
        self._changed(view.tenant_id)
        return view

    async def disconnect(
        self, actor: Actor, integration_id: UUID, command: RevisionCommand
    ) -> IntegrationView:
        self._cipher()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row = await self._locked(session, scope, integration_id)
            if row.status == "revoked" and row.revision == command.revision + 1:
                return await self._result(session, row)
            if row.revision != command.revision:
                raise ConflictError()
            row.active_version_id = None
            row.status = "revoked"
            row.revision += 1
            await session.flush()
            for item in await self._versions(session, row):
                # Erase every stored secret; this does not revoke the key at the provider.
                item.state = "retired" if item.state != "invalid" else "invalid"
                item.ciphertext, item.lease_token, item.lease_until = None, None, None
            for phone in await session.scalars(
                select(PhoneNumber)
                .where(
                    PhoneNumber.tenant_id == scope.tenant_id, PhoneNumber.integration_id == row.id
                )
                .with_for_update()
            ):
                phone.is_active = False
            self._audit(session, scope, "integration_disconnected", row.id)
            view = await self._result(session, row)
        self._changed(view.tenant_id)
        return view

    async def _telephony(
        self, session: AsyncSession, scope: _Scope, integration_id: UUID, cipher: SecretCipher
    ) -> tuple[TenantIntegration, dict[str, str]]:
        row = await self._locked(session, scope, integration_id)
        version = (
            await session.get(CredentialVersion, row.active_version_id)
            if row.active_version_id
            else None
        )
        if (
            row.purpose != Purpose.TELEPHONY
            or row.status != "verified"
            or row.account_identifier is None
            or version is None
            or version.ciphertext is None
        ):
            raise ConflictError()
        secrets: dict[str, str] = json.loads(
            cipher.decrypt(
                version.key_id,
                version.ciphertext,
                encryption_context(
                    scope.tenant_id, row.id, row.provider, row.purpose, version.version
                ),
            )
        )
        return row, secrets

    async def provider_numbers(
        self, actor: Actor, integration_id: UUID
    ) -> list[ProviderNumberView]:
        cipher = self._cipher()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row, secrets = await self._telephony(session, scope, integration_id, cipher)
            account = row.account_identifier or ""
            bound = set(
                await session.scalars(
                    select(PhoneNumber.number).where(
                        PhoneNumber.tenant_id == scope.tenant_id,
                        PhoneNumber.integration_id == row.id,
                        PhoneNumber.is_active.is_(True),
                    )
                )
            )
        try:
            numbers = await self._verifier.twilio_numbers(account, secrets)
        except ProviderUnavailableError:
            raise ProviderCheckUnavailableError() from None
        return [
            ProviderNumberView(
                number=item.number, provider_number_sid=item.sid, bound=item.number in bound
            )
            for item in numbers
        ]

    async def bind_number(
        self, actor: Actor, integration_id: UUID, command: BindNumber
    ) -> PhoneBindingView:
        cipher = self._cipher()
        async with self._sessions.begin() as session:
            scope = await self._enter(session, actor, write=True)
            row, secrets = await self._telephony(session, scope, integration_id, cipher)
            account, active_version = row.account_identifier or "", row.active_version_id
        try:
            # Ownership is proven by the provider, never by a pasted number.
            owned = await self._verifier.twilio_numbers(account, secrets, command.number)
        except ProviderUnavailableError:
            raise ProviderCheckUnavailableError() from None
        match = next((item for item in owned if item.number == command.number), None)
        if match is None:
            raise NumberNotOwnedError()
        try:
            async with self._sessions.begin() as session:
                scope = await self._enter(session, actor, write=True)
                row, _ = await self._telephony(session, scope, integration_id, cipher)
                if row.active_version_id != active_version:
                    raise ConflictError()
                phone = await session.scalar(
                    select(PhoneNumber)
                    .where(
                        PhoneNumber.tenant_id == scope.tenant_id,
                        PhoneNumber.number == command.number,
                    )
                    .with_for_update()
                )
                if phone is None:
                    phone = PhoneNumber(
                        id=uuid4(),
                        tenant_id=scope.tenant_id,
                        number=command.number,
                        integration_id=row.id,
                        provider_number_sid=match.sid,
                    )
                    session.add(phone)
                elif phone.integration_id is None and scope.platform_user_id is None:
                    # A number assigned on the platform account is moved by the platform only.
                    raise ConflictError()
                else:
                    phone.integration_id, phone.provider_number_sid = row.id, match.sid
                    phone.is_active = True
                await session.flush()
                self._audit(session, scope, "integration_number_bound", row.id)
                base = self._settings.telephony_public_base_url
                return PhoneBindingView(
                    id=phone.id,
                    tenant_id=phone.tenant_id,
                    integration_id=row.id,
                    number=phone.number,
                    provider_number_sid=match.sid,
                    is_active=phone.is_active,
                    voice_webhook_url=f"{base}/webhooks/v1/twilio/incoming",
                    status_callback_url=f"{base}/webhooks/v1/twilio/status",
                )
        except IntegrityError:
            # The number is already routed to another workspace.
            raise ConflictError() from None
