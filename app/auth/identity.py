"""Transaction-local identity scope; tenant membership checks remain separate."""

import hashlib

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.human_models import HumanIdentity


async def identity_scope(session: AsyncSession, email: str, *, serialize: bool = False) -> None:
    normalized = email.strip().lower()
    await session.execute(
        text("SELECT set_config('app.identity_email', :email, true)"), {"email": normalized}
    )
    if serialize:
        key = int.from_bytes(hashlib.sha256(normalized.encode()).digest()[:8], "big", signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def identity_for_email(
    session: AsyncSession, email: str, *, lock: bool = False
) -> HumanIdentity | None:
    await identity_scope(session, email, serialize=lock)
    query = select(HumanIdentity).where(HumanIdentity.email == email.strip().lower())
    return await session.scalar(query.with_for_update() if lock else query)
