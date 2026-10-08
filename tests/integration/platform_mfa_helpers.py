"""Use the real enrollment flow when a scenario needs verified platform access."""

import base64
import json
import secrets
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.platform.mfa import MfaCode, PlatformMfaService, totp
from app.platform.schemas import LoginCommand, SessionView
from app.platform.service import PlatformAuthenticationService


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
