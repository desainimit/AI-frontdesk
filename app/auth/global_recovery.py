"""Email-only recovery routes an existing mailbox proof to an accepted membership.

The encrypted receipt contains no reset authority. The single-use, bounded OTP
challenge and its global credential revision remain the authority for the write.
"""

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from pydantic import BaseModel, ConfigDict, RootModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.human import HumanAuthenticationService
from app.auth.human_models import HumanAccount
from app.auth.human_schemas import EmailIdentity, ResetPassword
from app.auth.identity import identity_for_email
from app.auth.workspace_models import IdentityMembership
from app.auth.workspace_schemas import GlobalEmail, GlobalPasswordReset, GlobalRecoveryView
from app.config.settings import Settings
from app.database.rls import set_tenant_scope, verify_runtime_role
from app.errors import AuthenticationError
from app.platform.throttle import AuthenticationUnavailableError
from app.security.encryption import SecretCipher
from app.tenants.models import Tenant
from app.users.models import User


class RecoveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: UUID
    challenge_id: UUID
    expires: int


class GlobalRecoveryService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings
        self.human = HumanAuthenticationService(sessions, settings)

    def cipher(self) -> SecretCipher:
        if not self.settings.tenant_human_auth_enabled or not self.settings.smtp_enabled:
            raise AuthenticationUnavailableError()
        try:
            return SecretCipher(
                self.settings.secret_encryption_keys, self.settings.secret_encryption_active_key
            )
        except ValueError:
            raise AuthenticationUnavailableError() from None

    @staticmethod
    def binding(email: str) -> str:
        return "global-password-recovery-v1:" + email

    async def accepted_workspace(self, email: str) -> UUID | None:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            identity = await identity_for_email(session, email)
            if identity is None:
                return None
            identity_id = identity.id
            memberships = [
                (row.tenant_id, row.user_id)
                for row in await session.scalars(
                    select(IdentityMembership)
                    .where(IdentityMembership.identity_id == identity.id)
                    .order_by(IdentityMembership.tenant_id)
                )
            ]
        for tenant_id, user_id in memberships:
            # Global discovery never relaxes tenant RLS or grants pending membership.
            async with self.sessions.begin() as session:
                await set_tenant_scope(session, tenant_id)
                tenant, user = (
                    await session.get(Tenant, tenant_id),
                    await session.get(User, user_id),
                )
                account = await session.scalar(
                    select(HumanAccount.id).where(
                        HumanAccount.user_id == user_id, HumanAccount.identity_id == identity_id
                    )
                )
                candidates = await session.scalar(
                    select(func.count()).select_from(User).where(func.lower(User.email) == email)
                )
                if (
                    tenant is not None
                    and tenant.is_active
                    and user is not None
                    and user.is_active
                    and account is not None
                    and candidates == 1
                ):
                    return tenant_id
        return None

    async def request(self, command: GlobalEmail, request_id: UUID) -> GlobalRecoveryView:
        cipher = self.cipher()  # Fail identically for known and unknown identities.
        tenant_id = await self.accepted_workspace(command.email)
        challenge_id = (
            await self.human.request_reset(
                EmailIdentity(tenant_id=tenant_id, email=command.email), request_id
            )
            if tenant_id is not None
            else None
        )
        expires = datetime.now(UTC) + timedelta(minutes=10)
        receipt = RecoveryReceipt(
            tenant_id=tenant_id or uuid4(),
            challenge_id=challenge_id or uuid4(),
            expires=int(expires.timestamp()),
        )
        key, ciphertext = cipher.encrypt(
            receipt.model_dump_json().encode(), self.binding(command.email)
        )
        token = base64.urlsafe_b64encode(
            json.dumps([key, base64.b64encode(ciphertext).decode()]).encode()
        ).decode()
        return GlobalRecoveryView(recovery_token=token, expires_at=expires)

    async def reset(self, command: GlobalPasswordReset, request_id: UUID) -> None:
        cipher = self.cipher()
        try:
            key, encoded = (
                RootModel[tuple[str, str]]
                .model_validate_json(
                    base64.b64decode(
                        command.recovery_token.get_secret_value(), altchars=b"-_", validate=True
                    ),
                    strict=True,
                )
                .root
            )
            receipt = RecoveryReceipt.model_validate_json(
                cipher.decrypt(
                    key, base64.b64decode(encoded, validate=True), self.binding(command.email)
                )
            )
            if receipt.expires <= int(datetime.now(UTC).timestamp()):
                raise ValueError()
        except (ValueError, TypeError, InvalidTag):
            raise AuthenticationError() from None
        await self.human.reset(
            ResetPassword(
                tenant_id=receipt.tenant_id,
                email=command.email,
                otp=command.otp,
                password=command.password,
            ),
            request_id,
            challenge_id=receipt.challenge_id,
        )
