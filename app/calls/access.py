"""Reception-assisted, tenant-configured, call-bound patient verification."""

import asyncio
import hashlib
import secrets
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.models import Call, PhoneNumber
from app.calls.service import CallContext, audit
from app.core.database.rls import set_tenant_scope, verify_runtime_role
from app.core.errors import AuthorizationError, ConflictError
from app.patients.models import Patient
from app.settings.store import load_settings
from app.tenants.models import Tenant
from app.users.models import User


class IssuePatientAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patient_id: UUID
    verification_attested: bool
    date_of_birth: date | None = None
    account_identifier: str | None = Field(
        default=None, min_length=4, max_length=64, pattern=r"^[A-Za-z0-9._-]+$"
    )


class PatientAccessCode(BaseModel):
    code: str
    expires_at: datetime


def normalize_access_code(value: str) -> str:
    """Gujarati decimal digits denote the same code; length and syntax stay strict."""
    return value.translate(str.maketrans("૦૧૨૩૪૫૬૭૮૯", "0123456789"))


class IdentifyPatient(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    access_code: str = Field(pattern=r"^[0-9]{8}$")

    @field_validator("access_code", mode="before")
    @classmethod
    def canonical_code(cls, value: object) -> object:
        return normalize_access_code(value) if isinstance(value, str) else value


async def active_call(session: AsyncSession, context: CallContext, *, lock: bool = False) -> Call:
    call = await session.scalar(
        select(Call)
        .join(Tenant, Tenant.id == Call.tenant_id)
        .join(PhoneNumber, PhoneNumber.id == Call.phone_number_id)
        .where(
            Call.id == context.call_id,
            Call.tenant_id == context.tenant_id,
            Call.provider_call_id == context.provider_call_id,
            Call.status == "active",
            Call.ended_at.is_(None),
            Tenant.is_active.is_(True),
            PhoneNumber.is_active.is_(True),
        )
        .with_for_update(read=not lock, of=Call)
    )
    if call is None:
        raise AuthorizationError()
    return call


def _digest(code: str, salt: str) -> str:
    return hashlib.scrypt(code.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


class CallPatientAccess:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def issue(
        self, context: RequestContext, call_id: UUID, command: IssuePatientAccess
    ) -> PatientAccessCode:
        require_permission(
            context,
            Permission.PATIENT_WRITE
            if context.actor_type == ActorType.PLATFORM_SUPPORT
            else Permission.PATIENT_READ,
        )
        if (
            context.actor_type not in {ActorType.USER, ActorType.PLATFORM_SUPPORT}
            or not command.verification_attested
        ):
            raise AuthorizationError()
        code, salt = f"{secrets.randbelow(100000000):08d}", secrets.token_hex(16)
        digest = await asyncio.to_thread(_digest, code, salt)
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            if context.actor_type == ActorType.PLATFORM_SUPPORT:
                from app.platform.support import validate_support_context

                await validate_support_context(session, context)
            else:
                user = await session.scalar(
                    select(User.id).where(
                        User.id == context.actor_id,
                        User.tenant_id == context.tenant_id,
                        User.is_active.is_(True),
                    )
                )
                if user is None:
                    raise AuthorizationError()
            provider_call_id = await session.scalar(
                select(Call.provider_call_id).where(Call.id == call_id)
            )
            if provider_call_id is None:
                raise AuthorizationError()
            binding = CallContext(context.tenant_id, call_id, provider_call_id, context.request_id)
            call = await active_call(session, binding, lock=True)
            policy = (await load_settings(session, context.tenant_id)).privacy.caller_verification
            if call.verified_patient_id is not None and (
                call.verification_expires_at is None
                or call.verification_expires_at <= datetime.now(UTC)
            ):
                call.verified_patient_id = None
                call.verification_expires_at = None
                call.access_patient_id = None
                call.access_attempts = 0
            if (
                call.verified_patient_id is not None
                or call.access_attempts >= policy.max_code_attempts
            ):
                raise ConflictError()
            patient = await session.scalar(
                select(Patient).where(
                    Patient.id == command.patient_id, Patient.tenant_id == context.tenant_id
                )
            )
            if patient is None:
                raise AuthorizationError()
            if "date_of_birth" in policy.required_factors and (
                patient.date_of_birth is None or patient.date_of_birth != command.date_of_birth
            ):
                raise AuthorizationError()
            if "account_identifier" in policy.required_factors and (
                patient.account_identifier is None
                or command.account_identifier is None
                or not secrets.compare_digest(
                    patient.account_identifier, command.account_identifier
                )
            ):
                raise AuthorizationError()
            call.access_patient_id = patient.id
            call.access_digest, call.access_salt = digest, salt
            expires_at = datetime.now(UTC) + timedelta(seconds=policy.code_ttl_seconds)
            call.access_expires_at = expires_at
            audit(session, binding, "call_patient_access_issued")
            # Preserve the real authenticated actor, including platform support access.
            from app.audit.models import AuditEvent

            session.add(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    actor_id=context.actor_id,
                    actor_type=context.actor_type,
                    platform_user_id=context.platform_user_id,
                    support_session_id=context.support_session_id,
                    action="call_patient_verification_attested",
                    resource_type="call",
                    resource_id=call_id,
                    request_id=context.request_id,
                    call_id=call_id,
                )
            )
            return PatientAccessCode(code=code, expires_at=expires_at)

    async def identify(self, context: CallContext, code: str) -> UUID | None:
        async with self.sessions.begin() as session:
            await verify_runtime_role(session)
            await set_tenant_scope(session, context.tenant_id)
            call = await active_call(session, context, lock=True)
            if call.verified_patient_id is not None:
                return (
                    call.verified_patient_id
                    if call.verification_expires_at is not None
                    and call.verification_expires_at > datetime.now(UTC)
                    else None
                )
            policy = (await load_settings(session, context.tenant_id)).privacy.caller_verification
            if call.access_attempts >= policy.max_code_attempts:
                return None
            call.access_attempts += 1
            valid = False
            if (
                call.access_digest
                and call.access_salt
                and call.access_expires_at
                and call.access_expires_at > datetime.now(UTC)
            ):
                digest = await asyncio.to_thread(_digest, code, call.access_salt)
                valid = secrets.compare_digest(digest, call.access_digest)
            if valid:
                call.verified_patient_id = call.access_patient_id
                call.verification_expires_at = datetime.now(UTC) + timedelta(
                    minutes=policy.access_ttl_minutes
                )
                call.access_digest = call.access_salt = None
                call.access_expires_at = None
            audit(
                session,
                context,
                "call_patient_identified" if valid else "call_patient_access_denied",
            )
            return call.verified_patient_id if valid else None
