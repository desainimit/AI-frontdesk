"""The operator's way back in when no platform administrator can sign in."""

import pytest
from sqlalchemy import select

from app.identity.models import Account, AccountSession
from app.identity.passwords import verify_password
from app.platform.models import PlatformAuditEvent
from app.platform.recover import recover_administrator
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy
from tests.integration.workspaces import create_platform_admin

NEW_PASSWORD = "a new long passphrase"


async def test_recovery_resets_one_administrator_and_nobody_else(tenancy: TenancyFixture) -> None:
    signed_in = await create_platform_admin(tenancy.admin)
    assert await recover_administrator(tenancy.admin, signed_in.email.upper(), NEW_PASSWORD)

    async with tenancy.admin.begin() as session:
        account = await session.get_one(Account, signed_in.account_id)
        old_session = await session.get_one(AccountSession, signed_in.session_id)
        recorded = await session.scalar(
            select(PlatformAuditEvent.action).where(
                PlatformAuditEvent.user_id == signed_in.account_id,
                PlatformAuditEvent.action == "super_admin_recovered",
            )
        )
    assert account.password_hash is not None
    assert verify_password(NEW_PASSWORD, account.password_hash)
    # Whoever was signed in with the old password is signed out.
    assert account.credential_revision == 2 and old_session.revoked_at is not None
    assert recorded == "super_admin_recovered"

    # A clinic's owner is not a platform administrator: nothing happens to them.
    async with tenancy.admin.begin() as session:
        owner = await session.scalar(
            select(Account.email).where(Account.is_platform_admin.is_(False)).limit(1)
        )
    assert owner is not None
    assert await recover_administrator(tenancy.admin, owner, NEW_PASSWORD) is False
    with pytest.raises(ValueError, match="12 to 128"):
        await recover_administrator(tenancy.admin, signed_in.email, "too short")
