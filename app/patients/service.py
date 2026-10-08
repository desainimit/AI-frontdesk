from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.database.search import contains
from app.database.unit_of_work import UnitOfWork
from app.errors import ConflictError, NotFoundError
from app.patients.models import Patient
from app.patients.schemas import CreatePatient, PatientView, UpdatePatientVerification


class PatientService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create(self, context: RequestContext, command: CreatePatient) -> PatientView:
        require_permission(context, Permission.PATIENT_WRITE)
        try:
            async with self._uow.transaction(context) as tx:
                entity = await tx.patients.add(
                    Patient(tenant_id=context.tenant_id, **command.model_dump())
                )
                AuditService(tx.audit).record(
                    context, AuditAction.PATIENT_CREATED, "patient", entity.id
                )
                return PatientView.model_validate(entity)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise ConflictError() from None
            raise

    async def get(self, context: RequestContext, patient_id: UUID) -> PatientView:
        require_permission(context, Permission.PATIENT_READ)
        async with self._uow.transaction(context) as tx:
            entity = await tx.patients.get(patient_id)
            if entity is None:
                raise NotFoundError()
            AuditService(tx.audit).record(context, AuditAction.PATIENT_VIEWED, "patient", entity.id)
            return PatientView.model_validate(entity)

    async def set_verification(
        self, context: RequestContext, patient_id: UUID, command: UpdatePatientVerification
    ) -> PatientView:
        require_permission(context, Permission.PATIENT_WRITE)
        try:
            async with self._uow.transaction(context) as tx:
                entity = await tx.patients.get(patient_id, lock=True)
                if entity is None:
                    raise NotFoundError()
                entity.date_of_birth = command.date_of_birth
                entity.account_identifier = command.account_identifier
                await tx.patients.flush()
                await tx.patients.revoke_call_access(patient_id)
                AuditService(tx.audit).record(
                    context, AuditAction.PATIENT_VERIFICATION_UPDATED, "patient", entity.id
                )
                return PatientView.model_validate(entity)
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise ConflictError() from None
            raise

    async def list(
        self, context: RequestContext, limit: int, offset: int, q: str | None = None
    ) -> list[PatientView]:
        require_permission(context, Permission.PATIENT_READ)
        async with self._uow.transaction(context) as tx:
            entities = await tx.patients.list(
                limit, offset, contains(q, Patient.display_name, Patient.phone)
            )
            AuditService(tx.audit).record(context, AuditAction.PATIENTS_VIEWED, "patients")
            return [PatientView.model_validate(row) for row in entities]
