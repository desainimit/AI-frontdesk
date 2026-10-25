"""Names a caller is likely to say, so speech recognition hears them right.

Clinic, provider, service and location names are unusual words that general speech
models often get wrong ("Dr. Mehta" heard as "doctor matter"). Deepgram is told them
at the start of the call.
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clinic.models import Service
from app.core.database.rls import set_tenant_scope
from app.locations.models import Location
from app.providers.models import Provider
from app.settings.schemas import ClinicConfiguration

logger = logging.getLogger(__name__)

MAX_TERMS = 100
MAX_TERM_LENGTH = 60


async def call_keyterms(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    configuration: ClinicConfiguration,
) -> tuple[str, ...]:
    terms = [configuration.clinic.name]
    try:
        async with sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            providers = await session.scalars(
                select(Provider.display_name)
                .where(Provider.tenant_id == tenant_id, Provider.is_active.is_(True))
                .limit(40)
            )
            terms.extend(providers)
            services = await session.scalars(
                select(Service.name)
                .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
                .limit(40)
            )
            terms.extend(services)
            locations = await session.scalars(
                select(Location.name).where(Location.tenant_id == tenant_id).limit(10)
            )
            terms.extend(locations)
    except Exception:
        # Optional: recognition still works without them, only less well on names.
        logger.warning("call_keyterms_unavailable", extra={"tenant_id": str(tenant_id)})
    clean = (" ".join(term.split()) for term in terms if term)
    unique = dict.fromkeys(term for term in clean if 1 < len(term) <= MAX_TERM_LENGTH)
    return tuple(unique)[:MAX_TERMS]
