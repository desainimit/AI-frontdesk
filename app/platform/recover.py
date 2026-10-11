"""Let a platform administrator back in when nobody can sign in any more.

    RECOVERY_PASSWORD='a new long password' python -m app.platform.recover admin@example.com

Run by an operator with the schema owner's database login, never from the product. It
sets a new password, turns two-step verification off, signs the account out everywhere
and records that it happened. It only works for an existing platform administrator: it
creates nobody and promotes nobody. The password comes from the environment so it is
not left in the shell history or the process list.
"""

import asyncio
import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.settings import Settings
from app.identity.models import Account, AccountMfa, AccountSession
from app.identity.passwords import hash_password
from app.platform.models import PlatformAuditEvent

MIN_PASSWORD, MAX_PASSWORD = 12, 128


async def recover_administrator(
    sessions: async_sessionmaker[AsyncSession], email: str, password: str
) -> bool:
    """True when the account was reset; False when there is no such administrator."""
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise ValueError("The new password needs 12 to 128 characters")
    digest = await asyncio.to_thread(hash_password, password)
    async with sessions.begin() as session:
        account = await session.scalar(
            select(Account)
            .where(Account.email == email.strip().lower(), Account.is_platform_admin.is_(True))
            .with_for_update()
        )
        if account is None:
            return False
        account.password_hash = digest
        account.is_active = True
        # Every session and reset link issued before now stops working.
        account.credential_revision += 1
        await session.execute(delete(AccountMfa).where(AccountMfa.account_id == account.id))
        await session.execute(
            update(AccountSession)
            .where(AccountSession.account_id == account.id, AccountSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        session.add(
            PlatformAuditEvent(
                user_id=account.id, action="super_admin_recovered", request_id=uuid4()
            )
        )
        return True


async def _run(settings: Settings, email: str, password: str) -> bool:
    if settings.migration_database_url is None:
        raise ValueError("Migration credentials required")
    engine = create_async_engine(settings.migration_url, poolclass=NullPool, hide_parameters=True)
    try:
        return await recover_administrator(
            async_sessionmaker(engine, expire_on_commit=False), email, password
        )
    finally:
        await engine.dispose()


def main() -> None:
    password = os.environ.get("RECOVERY_PASSWORD", "")
    if len(sys.argv) != 2 or not password:
        raise SystemExit(
            "Usage: RECOVERY_PASSWORD='<new password>' python -m app.platform.recover <email>"
        )
    try:
        done = asyncio.run(_run(Settings.from_environment(), sys.argv[1], password))
    except (ValueError, SQLAlchemyError) as failure:
        reason = str(failure) if isinstance(failure, ValueError) else "the database refused it"
        raise SystemExit(f"Recovery failed: {reason}.") from None
    if not done:
        raise SystemExit("No platform administrator has that email. Nothing was changed.")
    print(
        "Password replaced, two-step verification turned off, signed out everywhere. "
        "Sign in and turn two-step verification on again."
    )


if __name__ == "__main__":
    main()
