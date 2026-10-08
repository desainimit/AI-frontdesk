"""Use the real enrollment flow when a scenario needs verified platform access."""

import base64
import json
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.platform.bootstrap import seed_super_admin
from app.platform.mfa import MfaCode, PlatformMfaService, totp
from app.platform.policies import PlatformContext
from app.platform.schemas import LoginCommand, SessionView
from app.platform.service import PlatformAuthenticationService

if TYPE_CHECKING:
    from tests.integration.test_tenancy import TenancyFixture


def mfa_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "secret_encryption_keys": SecretStr(
                json.dumps(
                    {
                        "mfa-test": base64.b64encode(secrets.token_bytes(32)).decode(),
                    }
                )
            ),
            "secret_encryption_active_key": "mfa-test",
        }
    )


async def mfa_login(
    service: PlatformAuthenticationService,
    command: LoginCommand,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession] | None = None,
) -> SessionView:
    login = await service.login(command, uuid4())
    context = await service.authenticate(login.access_token, uuid4())
    mfa = PlatformMfaService(sessions or service.sessions, mfa_settings(settings))
    enrollment = await mfa.begin(context)
    await mfa.confirm(
        context,
        MfaCode(code=SecretStr(totp(enrollment.secret, int(datetime.now(UTC).timestamp()) // 30))),
    )
    return login.model_copy(update={"mfa_verified": True})


async def platform(tenancy: "TenancyFixture") -> PlatformContext:
    """A signed-in, MFA-verified super admin for scenarios that act on tenants."""
    from tests.integration.test_platform import seed_command

    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    settings = mfa_settings(tenancy.settings)
    authentication = PlatformAuthenticationService(tenancy.database.session_factory, settings)
    login = await mfa_login(authentication, command, settings)
    return await authentication.authenticate(login.access_token, uuid4())
