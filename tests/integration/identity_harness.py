"""Shared helpers for tests of sign-in, invitations and platform administrators."""

import base64
import json
import re
import secrets
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import select

from app.auth.context import RequestContext
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.identity.invitations import InvitationService
from app.identity.mfa import MfaService
from app.identity.models import AuthChallenge, AuthMail
from app.identity.schemas import AcceptInvitation, Login, OpenWorkspace, SessionView
from app.identity.service import IdentityService
from app.identity.sessions import authenticate_platform, authenticate_workspace
from app.platform.onboarding import OnboardingDraft, OnboardingView, TenantOnboardingService
from app.platform.policies import PlatformContext
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.workspaces import PASSWORD, PlatformAdmin, create_platform_admin


def mail_settings(base: Settings) -> Settings:
    """Settings under which invitations and resets can be queued (nothing is ever sent)."""
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    return base.model_copy(
        update={
            "tenant_auth_public_origin": "https://app.example.test",
            "secret_encryption_keys": SecretStr(json.dumps({"k1": key})),
            "secret_encryption_active_key": "k1",
            "smtp_host": "smtp.example.test",
            "smtp_sender": "noreply@example.test",
        }
    )


class IdentityHarness:
    def __init__(self, tenancy: TenancyFixture) -> None:
        self.tenancy = tenancy
        self.settings = mail_settings(tenancy.settings)
        self.sessions = tenancy.database.session_factory
        self.identity = IdentityService(self.sessions, self.settings)
        self.invitations = InvitationService(self.sessions, self.settings)
        self.mfa = MfaService(self.sessions, self.settings)
        self.cipher = SecretCipher(self.settings.secret_encryption_keys, "k1")

    async def admin(self) -> PlatformAdmin:
        return await create_platform_admin(self.tenancy.admin)

    async def platform(self, admin: PlatformAdmin | None = None) -> PlatformContext:
        """A signed-in platform administrator's context."""
        admin = admin or await self.admin()
        return await authenticate_platform(self.sessions, admin.token.get_secret_value(), uuid4())

    async def open(
        self, admin: PlatformAdmin, tenant_id: UUID, reason: str = "Customer asked for help"
    ) -> RequestContext:
        """The administrator enters a workspace; returns the context their requests carry."""
        await self.identity.open_workspace(
            await self.platform(admin), tenant_id, OpenWorkspace(reason=reason)
        )
        return await authenticate_workspace(self.sessions, admin.token.get_secret_value(), uuid4())

    async def onboard(self, context: PlatformContext, email: str) -> OnboardingView:
        draft = OnboardingDraft.model_validate(
            {
                "name": f"Clinic {uuid4()}",
                "administrator": {"email": email, "display_name": "Clinic Admin"},
                "location": {"name": "Main", "timezone": "Asia/Kolkata"},
                "language_policy": {"default_language": "en", "enabled_languages": ["en"]},
            }
        )
        return await TenantOnboardingService(self.sessions).create(context, draft, str(uuid4()))

    async def mail(
        self, *, user_id: UUID | None = None, email: str | None = None
    ) -> dict[str, str]:
        """The newest queued message for a membership or an address, read as the worker would."""
        async with self.tenancy.admin.begin() as session:
            query = (
                select(AuthMail)
                .join(AuthChallenge, AuthChallenge.id == AuthMail.challenge_id)
                .where(AuthMail.ciphertext.is_not(None))
                .order_by(AuthMail.created_at.desc())
            )
            if user_id is not None:
                query = query.where(AuthChallenge.user_id == user_id)
            rows = (await session.scalars(query)).all()
            for row in rows:
                assert row.ciphertext is not None
                payload: dict[str, str] = json.loads(
                    self.cipher.decrypt(row.key_id, row.ciphertext, f"auth-mail:{row.id}")
                )
                if email is None or payload["to"] == email:
                    return payload
        raise AssertionError("no queued message")

    async def link_token(self, *, user_id: UUID | None = None, email: str | None = None) -> str:
        """The single-use token inside the newest emailed link."""
        body = (await self.mail(user_id=user_id, email=email))["body"]
        match = re.search(r"#token=(\S+)", body)
        assert match is not None
        return match.group(1)

    async def accept(self, token: str, password: str | None = PASSWORD) -> SessionView | None:
        return await self.invitations.accept(
            AcceptInvitation(
                token=SecretStr(token), password=SecretStr(password) if password else None
            ),
            uuid4(),
        )

    async def login(self, email: str, password: str = PASSWORD) -> SessionView:
        return await self.identity.login(Login(email=email, password=SecretStr(password)), uuid4())

    async def context(self, token: str) -> RequestContext:
        return await authenticate_workspace(self.sessions, token, uuid4())
