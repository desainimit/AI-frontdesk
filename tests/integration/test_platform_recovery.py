"""Recovery codes never replace the password or disable MFA without a fresh proof."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select, update

from app.core.errors import AuthenticationError, ConflictError
from app.platform.bootstrap import seed_super_admin
from app.platform.mfa import MfaCode, PlatformMfaService, RecoveryIssue, RecoveryReplace, totp
from app.platform.models import PlatformAuthenticator
from app.platform.schemas import LoginCommand
from app.platform.service import PlatformAuthenticationService, validate_context
from app.platform.support import EnterSupportCommand, SupportAccessService
from tests.integration.platform_mfa_helpers import mfa_settings
from tests.integration.test_platform import seed_command
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


async def test_codes_are_hash_only_single_use_and_revoke_old_sessions(
    tenancy: TenancyFixture,
) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    settings = mfa_settings(tenancy.settings)
    auth = PlatformAuthenticationService(tenancy.database.session_factory, settings)
    original = await auth.login(command, uuid4())
    context = await auth.authenticate(original.access_token, uuid4())
    mfa = PlatformMfaService(tenancy.database.session_factory, settings)
    enrollment = await mfa.begin(context)
    codes = await mfa.confirm(
        context,
        MfaCode(code=SecretStr(totp(enrollment.secret, int(datetime.now(UTC).timestamp()) // 30))),
    )
    assert len(codes.codes) == len(set(codes.codes)) == 8 and codes.revision == 1
    assert codes.codes[0] not in repr(codes)
    support_service = SupportAccessService(tenancy.database.session_factory)
    elevated = await auth.authenticate(original.access_token, uuid4())
    support = await support_service.enter(
        elevated,
        EnterSupportCommand(
            tenant_id=tenancy.a.tenant_id, reason="Verify recovery revokes tenant entry"
        ),
    )
    async with tenancy.admin.begin() as session:
        factor = await session.scalar(
            select(PlatformAuthenticator).where(PlatformAuthenticator.user_id == context.user_id)
        )
        assert factor and all(
            len(digest) == 64 and digest not in codes.codes for digest in factor.recovery_digests
        )
    wrong = LoginCommand(
        email=command.email,
        password=SecretStr("Wrong password"),
        recovery_code=SecretStr(codes.codes[0]),
    )
    with pytest.raises(AuthenticationError):
        await auth.login(wrong, uuid4())
    assert (await mfa.recovery_status(context)).remaining == 8
    proof = LoginCommand(
        email=command.email, password=command.password, recovery_code=SecretStr(codes.codes[0])
    )
    results = await asyncio.gather(
        auth.login(proof, uuid4()), auth.login(proof, uuid4()), return_exceptions=True
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, AuthenticationError) for result in results) == 1
    recovered = next(result for result in results if not isinstance(result, BaseException))
    assert recovered.mfa_verified and recovered.mfa_recovered
    with pytest.raises(AuthenticationError):
        await auth.authenticate(original.access_token, uuid4())
    with pytest.raises(AuthenticationError):
        await support_service.authenticate(support.access_token, uuid4())
    current = await auth.authenticate(recovered.access_token, uuid4())
    assert (await mfa.recovery_status(current)).remaining == 7
    with pytest.raises(AuthenticationError):
        await mfa.replace_factor(
            current,
            RecoveryReplace(
                password=SecretStr("Wrong password"), recovery_code=SecretStr(codes.codes[1])
            ),
        )
    assert (await mfa.recovery_status(current)).remaining == 7
    await mfa.replace_factor(
        current, RecoveryReplace(password=command.password, recovery_code=SecretStr(codes.codes[1]))
    )
    async with tenancy.database.session_factory.begin() as session:
        with pytest.raises(AuthenticationError):
            await validate_context(session, current)
    replacement = await mfa.begin(current)
    assert replacement.secret != enrollment.secret
    new_codes = await mfa.confirm(
        current,
        MfaCode(code=SecretStr(totp(replacement.secret, int(datetime.now(UTC).timestamp()) // 30))),
    )
    assert len(new_codes.codes) == 8 and set(new_codes.codes).isdisjoint(codes.codes)
    with pytest.raises(AuthenticationError):
        await auth.login(
            LoginCommand(
                email=command.email,
                password=command.password,
                recovery_code=SecretStr(codes.codes[2]),
            ),
            uuid4(),
        )


async def test_rotation_requires_fresh_totp_revision_and_shared_lockout(
    tenancy: TenancyFixture,
) -> None:
    command = seed_command()
    await seed_super_admin(tenancy.admin, command)
    settings = mfa_settings(tenancy.settings)
    auth = PlatformAuthenticationService(tenancy.database.session_factory, settings)
    original = await auth.login(command, uuid4())
    context = await auth.authenticate(original.access_token, uuid4())
    mfa = PlatformMfaService(tenancy.database.session_factory, settings)
    enrollment = await mfa.begin(context)
    counter = int(datetime.now(UTC).timestamp()) // 30
    codes = await mfa.confirm(context, MfaCode(code=SecretStr(totp(enrollment.secret, counter))))
    with pytest.raises(AuthenticationError):
        await mfa.recovery_issue(
            context,
            RecoveryIssue(
                revision=codes.revision, code=SecretStr(totp(enrollment.secret, counter))
            ),
        )
    async with tenancy.admin.begin() as session:
        await session.execute(
            update(PlatformAuthenticator)
            .where(PlatformAuthenticator.user_id == context.user_id)
            .values(last_counter=counter - 1)
        )
    rotated = await mfa.recovery_issue(
        context,
        RecoveryIssue(revision=codes.revision, code=SecretStr(totp(enrollment.secret, counter))),
    )
    assert rotated.revision > codes.revision and set(rotated.codes).isdisjoint(codes.codes)
    with pytest.raises(ConflictError):
        await mfa.recovery_issue(
            context,
            RecoveryIssue(
                revision=codes.revision, code=SecretStr(totp(enrollment.secret, counter))
            ),
        )
    for _ in range(5):
        with pytest.raises(AuthenticationError):
            await auth.login(
                LoginCommand(
                    email=command.email,
                    password=command.password,
                    recovery_code=SecretStr(codes.codes[0]),
                ),
                uuid4(),
            )
    with pytest.raises(AuthenticationError):
        await auth.login(
            LoginCommand(
                email=command.email,
                password=command.password,
                recovery_code=SecretStr(rotated.codes[0]),
            ),
            uuid4(),
        )
    async with tenancy.admin.begin() as session:
        factor = await session.scalar(
            select(PlatformAuthenticator).where(PlatformAuthenticator.user_id == context.user_id)
        )
        assert factor and factor.locked_until and factor.locked_until > datetime.now(UTC)
        factor.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    assert (
        await auth.login(
            LoginCommand(
                email=command.email,
                password=command.password,
                recovery_code=SecretStr(rotated.codes[0]),
            ),
            uuid4(),
        )
    ).mfa_recovered
