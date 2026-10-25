"""The clinic's doctors, services and locations, read once when a call starts.

Two uses: speech recognition is told the names so it hears them right ("Dr. Mehta"
heard as "doctor matter"), and the model is given the list with ids so it can check a
doctor's diary at once instead of first looking up who works there.
"""

import logging
from dataclasses import dataclass, field
from typing import Any
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
MAX_LISTED = 40


@dataclass(frozen=True)
class ClinicDirectory:
    clinic: str
    providers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    services: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    locations: list[str] = field(default_factory=list[str])

    @property
    def keyterms(self) -> tuple[str, ...]:
        """Names a caller is likely to say, for speech recognition."""
        names = [
            self.clinic,
            *(str(item["name"]) for item in self.providers),
            *(str(item["name"]) for item in self.services),
            *self.locations,
        ]
        clean = (" ".join(name.split()) for name in names if name)
        unique = dict.fromkeys(name for name in clean if 1 < len(name) <= MAX_TERM_LENGTH)
        return tuple(unique)[:MAX_TERMS]

    @property
    def for_model(self) -> dict[str, Any]:
        """Doctors and services with their ids, so the model can book without a lookup."""
        return {"doctors": self.providers, "services": self.services}


async def clinic_directory(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    configuration: ClinicConfiguration,
) -> ClinicDirectory:
    try:
        async with sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            providers = await session.execute(
                select(Provider.id, Provider.display_name, Provider.specialty)
                .where(Provider.tenant_id == tenant_id, Provider.is_active.is_(True))
                .order_by(Provider.display_name)
                .limit(MAX_LISTED)
            )
            services = await session.execute(
                select(Service.id, Service.name, Service.duration_minutes)
                .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
                .order_by(Service.name)
                .limit(MAX_LISTED)
            )
            locations = await session.scalars(
                select(Location.name).where(Location.tenant_id == tenant_id).limit(10)
            )
            return ClinicDirectory(
                configuration.clinic.name,
                [
                    {"id": str(row.id), "name": row.display_name, "specialty": row.specialty or ""}
                    for row in providers
                ],
                [
                    {"id": str(row.id), "name": row.name, "minutes": row.duration_minutes}
                    for row in services
                ],
                list(locations),
            )
    except Exception:
        # Optional: the model can still look them up, and recognition still works,
        # only less well on names.
        logger.warning("clinic_directory_unavailable", extra={"tenant_id": str(tenant_id)})
        return ClinicDirectory(configuration.clinic.name)
