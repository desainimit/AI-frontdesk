from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.calls.models import Call
from app.database.domain import ScopedRepository
from app.patients.models import Patient


class PatientRepository(ScopedRepository[Patient]):
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        super().__init__(Patient, session, context)

    async def display_names(self, ids: set[UUID]) -> dict[UUID, str]:
        if not ids:
            return {}
        rows = await self._session.execute(
            select(Patient.id, Patient.display_name).where(
                Patient.tenant_id == self._context.tenant_id, Patient.id.in_(ids)
            )
        )
        return {row[0]: row[1] for row in rows}

    async def revoke_call_access(self, patient_id: UUID) -> None:
        await self._session.execute(
            update(Call)
            .where(
                Call.tenant_id == self._context.tenant_id,
                Call.status == "active",
                or_(
                    Call.access_patient_id == patient_id,
                    Call.verified_patient_id == patient_id,
                ),
            )
            .values(
                access_patient_id=None,
                verified_patient_id=None,
                access_digest=None,
                access_salt=None,
                access_expires_at=None,
                verification_expires_at=None,
                access_attempts=0,
            )
        )
