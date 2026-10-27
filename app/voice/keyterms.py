"""The clinic's doctors, services and locations, read once when a call starts.

Two uses: speech recognition is told the names so it hears them right ("Dr. Mehta"
heard as "doctor matter"), and the model is given the list with ids so it can check a
doctor's diary at once instead of first looking up who works there.

Recognition is also told the names of the patients this caller is likely to be: those
on the calling number, and those with an appointment soon (who call to move it). Those
names go to speech recognition only, never to the model.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.romanize import latin_fold
from app.appointments.models import Appointment
from app.clinic.models import Service
from app.core.database.rls import set_tenant_scope
from app.locations.models import Location
from app.patients.models import Patient
from app.providers.models import Provider
from app.settings.schemas import ClinicConfiguration

logger = logging.getLogger(__name__)

MAX_TERMS = 100
MAX_TERM_LENGTH = 60
MAX_LISTED = 40
# Patients with an appointment in this window are the likeliest callers.
EXPECTED_FROM = timedelta(hours=2)
EXPECTED_UNTIL = timedelta(days=3)
MAX_EXPECTED = 40


@dataclass(frozen=True)
class ClinicDirectory:
    clinic: str
    providers: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    services: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    locations: list[str] = field(default_factory=list[str])
    # For recognition only: the caller's own names on file, then others expected soon.
    callers: list[str] = field(default_factory=list[str])
    expected: list[str] = field(default_factory=list[str])

    @property
    def keyterms(self) -> tuple[str, ...]:
        """Names a caller is likely to say, for speech recognition."""
        # Most important first: the list is cut at MAX_TERMS.
        names = [
            self.clinic,
            *spellings(self.callers),
            *(str(item["name"]) for item in self.providers),
            *(str(item["name"]) for item in self.services),
            *self.locations,
            *spellings(self.expected),
        ]
        clean = (" ".join(name.split()) for name in names if name)
        unique = dict.fromkeys(name for name in clean if 1 < len(name) <= MAX_TERM_LENGTH)
        return tuple(unique)[:MAX_TERMS]

    @property
    def for_model(self) -> dict[str, Any]:
        """Doctors and services with their ids, so the model can book without a lookup."""
        return {"doctors": self.providers, "services": self.services}


def spellings(names: list[str]) -> list[str]:
    """Each name as stored and, for one in Hindi or Gujarati script, in Latin letters:
    the caller may be transcribed in either ("निमित देसाई", "Nimit Desai")."""
    out: list[str] = []
    for name in names:
        out.append(name)
        latin = latin_fold(name)
        if latin != name.casefold() and latin.isascii():
            out.append(latin.title())
    return out


async def expected_callers(
    sessions: async_sessionmaker[AsyncSession], tenant_id: UUID, caller_number: str | None
) -> tuple[list[str], list[str]]:
    """The names on the calling number, and those of patients with an appointment soon."""
    try:
        async with sessions.begin() as session:
            await set_tenant_scope(session, tenant_id)
            callers = (
                await session.scalars(
                    select(Patient.display_name)
                    .where(Patient.tenant_id == tenant_id, Patient.phone == caller_number)
                    .limit(5)
                )
                if caller_number
                else ()
            )
            now = datetime.now(UTC)
            expected = await session.scalars(
                select(Patient.display_name)
                .join(
                    Appointment,
                    (Appointment.tenant_id == Patient.tenant_id)
                    & (Appointment.patient_id == Patient.id),
                )
                .where(
                    Patient.tenant_id == tenant_id,
                    Appointment.status == "booked",
                    Appointment.starts_at >= now - EXPECTED_FROM,
                    Appointment.starts_at <= now + EXPECTED_UNTIL,
                )
                .order_by(Appointment.starts_at)
                .limit(MAX_EXPECTED)
            )
            return list(callers), list(dict.fromkeys(expected))
    except Exception:
        # Optional: recognition still works, only less well on names.
        logger.warning("expected_callers_unavailable", extra={"tenant_id": str(tenant_id)})
        return [], []


async def clinic_directory(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    configuration: ClinicConfiguration,
    caller_number: str | None = None,
) -> ClinicDirectory:
    callers, expected = await expected_callers(sessions, tenant_id, caller_number)
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
                callers,
                expected,
            )
    except Exception:
        # Optional: the model can still look them up, and recognition still works,
        # only less well on names.
        logger.warning("clinic_directory_unavailable", extra={"tenant_id": str(tenant_id)})
        return ClinicDirectory(configuration.clinic.name, callers=callers, expected=expected)
