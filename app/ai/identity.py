"""Who the person is, decided by the server and never by what the model was told.

A number that belongs to exactly one patient identifies that patient. A number the
clinic does not know can be registered as a new patient. On the phone, reception can
also issue a short code (see `CallPatientAccess`).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.caller_check import names_match
from app.ai.session import Session
from app.appointments.models import Appointment
from app.calls.models import Call
from app.conversation.state import (
    ConversationState,
    IdentifierType,
    IdentityState,
    VerificationStatus,
)
from app.core.database.unit_of_work import enter_tenant_scope
from app.messaging.models import Conversation
from app.patients.models import Patient
from app.settings.store import load_settings
from app.tickets.service import open_ticket


class ContactIdentity:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def recognise(self, session: Session, state: ConversationState) -> bool:
        """Tie the conversation to the one patient whose number this is, if there is one."""
        if state.identity_state.is_verified:
            return True
        if not session.contact or state.identity_state.status == VerificationStatus.FAILED:
            return False
        async with self._sessions.begin() as database:
            await enter_tenant_scope(database, session.request(state))
            matches = (
                await database.scalars(
                    select(Patient.id)
                    .where(Patient.tenant_id == session.tenant_id, Patient.phone == session.contact)
                    .limit(2)
                )
            ).all()
            if len(matches) != 1:
                # Nobody, or a number shared by several patients: not enough to go on.
                return False
            await self._bind(database, session, matches[0])
        self._remember(state, matches[0])
        return True

    async def confirm(
        self, session: Session, state: ConversationState, name: str, number: int | None
    ) -> tuple[UUID, str] | None:
        """The patient and their name on file when the caller's details match them.

        With an appointment number: the patient that appointment belongs to, if the name
        is theirs. Without one: the patient this number already belongs to, if the name
        is theirs. Nothing is told about a mismatch, so it cannot be used to probe.
        """
        identity = state.identity_state
        async with self._sessions.begin() as database:
            await enter_tenant_scope(database, session.request(state))
            if number is not None:
                patient_id = await database.scalar(
                    select(Appointment.patient_id).where(
                        Appointment.tenant_id == session.tenant_id, Appointment.number == number
                    )
                )
            else:
                patient_id = identity.patient_id if identity.is_verified else None
            if patient_id is None:
                return None
            on_file = await database.scalar(
                select(Patient.display_name).where(
                    Patient.tenant_id == session.tenant_id, Patient.id == patient_id
                )
            )
            if on_file is None or not names_match(name, on_file):
                return None
            await self._bind(database, session, patient_id)
        return patient_id, on_file

    async def register(
        self, session: Session, state: ConversationState, name: str, *, unsure: bool = False
    ) -> bool:
        """Add the person as a new patient under the number they are using. `unsure`: the
        caller could not confirm the name as heard, so the team is asked to check it."""
        if not session.contact or not session.writes:
            return False
        async with self._sessions.begin() as database:
            await enter_tenant_scope(database, session.request(state))
            patient = Patient(
                tenant_id=session.tenant_id,
                display_name=name.strip()[:200],
                phone=session.contact,
                language=state.language[:10],
            )
            database.add(patient)
            await database.flush()
            await self._bind(database, session, patient.id)
            if unsure:
                on_call = session.provider_call_id is not None
                await open_ticket(
                    database,
                    session.tenant_id,
                    source="call" if on_call else "sms",
                    subject="Check a new patient's name",
                    description=(
                        f'Registered on a call as "{patient.display_name}". The caller could '
                        "not confirm the spelling; please check it with them."
                    ),
                    patient_id=patient.id,
                    contact=session.contact,
                    call_id=session.conversation_id if on_call else None,
                    conversation_id=None if on_call else session.conversation_id,
                    author="assistant",
                )
        self._remember(state, patient.id)
        return True

    @staticmethod
    async def _bind(database: AsyncSession, session: Session, patient_id: UUID) -> None:
        if session.provider_call_id is None:
            await database.execute(
                update(Conversation)
                .where(
                    Conversation.tenant_id == session.tenant_id,
                    Conversation.id == session.conversation_id,
                )
                .values(patient_id=patient_id)
            )
            return
        policy = (await load_settings(database, session.tenant_id)).privacy.caller_verification
        await database.execute(
            update(Call)
            .where(Call.tenant_id == session.tenant_id, Call.id == session.conversation_id)
            .values(
                verified_patient_id=patient_id,
                verification_expires_at=datetime.now(UTC)
                + timedelta(minutes=policy.access_ttl_minutes),
            )
        )

    @staticmethod
    def _remember(state: ConversationState, patient_id: UUID) -> None:
        identity = IdentityState(caller_phone=state.identity_state.caller_phone)
        identity.record_attempt(True, patient_id)
        identity.identifier_type = IdentifierType.PHONE
        state.identity_state = identity
