from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.search import contains
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import ConflictError, NotFoundError
from app.patients.models import Patient
from app.patients.schemas import (
    CreatePatient,
    PatientView,
    UpdatePatient,
    UpdatePatientVerification,
)


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

    async def update(
        self, context: RequestContext, patient_id: UUID, command: UpdatePatient
    ) -> PatientView:
        require_permission(context, Permission.PATIENT_WRITE)
        async with self._uow.transaction(context) as tx:
            entity = await tx.patients.get(patient_id, lock=True)
            if entity is None:
                raise NotFoundError()
            previous_phone = entity.phone
            for field, value in command.model_dump().items():
                setattr(entity, field, value)
            await tx.patients.flush()
            if command.sms_opt_out:
                # Texts already waiting, such as a reminder, must not go out either.
                await tx.messages.cancel_texts_to(previous_phone)
            AuditService(tx.audit).record(
                context, AuditAction.PATIENT_UPDATED, "patient", entity.id
            )
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
