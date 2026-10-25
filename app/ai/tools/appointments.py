"""Appointment proposals and their confirmation, decided by the server, not the model.

The model may only propose. The server words the question, waits for a plain yes, and
only then writes. A test chat proposes nothing and writes nothing.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.ai.identity import ContactIdentity
from app.ai.session import AssistantAuthorizationError, Session
from app.ai.wording import spoken
from app.appointments.availability import AvailabilityService
from app.appointments.commands import (
    BookAppointment,
    CancelAppointment,
    RescheduleAppointment,
    SlotQuery,
)
from app.appointments.service import AppointmentService
from app.calendar_sync.service import CalendarService
from app.calls.access import CallPatientAccess, IdentifyPatient, normalize_access_code
from app.conversation.confirmation import confirmation_answer
from app.conversation.manager import ConversationStateManager
from app.conversation.state import (
    AppointmentDraft,
    AppointmentOperation,
    ConfirmationStatus,
    ConversationState,
    FlowStep,
    FlowType,
    IdentifierType,
    IdentityState,
    VerificationStatus,
)
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import ApplicationError
from app.notifications.templates import when

Action = Literal["book_appointment", "cancel_appointment", "reschedule_appointment"]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NoInput(ToolInput):
    pass


class RegisterInput(ToolInput):
    full_name: str = Field(min_length=2, max_length=120, description="As the person gave it.")


class AvailabilityInput(ToolInput):
    provider_id: UUID
    # From list_services; sets the slot length. Leave out for the clinic's usual length.
    service_id: UUID | None = None
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class BookInput(ToolInput):
    provider_id: UUID
    service_id: UUID | None = None
    starts_at: AwareDatetime


class CancelInput(ToolInput):
    appointment_id: UUID


class RescheduleInput(CancelInput):
    starts_at: AwareDatetime


class Outcome(BaseModel):
    """What a tool tells the model. `say` is server wording the person must hear as is."""

    status: str
    say: str = ""
    next: str = ""
    items: list[dict[str, Any]] = Field(default_factory=lambda: list[dict[str, Any]]())


@dataclass(frozen=True)
class Direct:
    """A reply the server gives itself, without asking the model."""

    response: str
    tool: str


class AppointmentFlow:
    def __init__(
        self,
        uow: UnitOfWork,
        access: CallPatientAccess,
        identity: ContactIdentity,
        calendar: CalendarService | None = None,
    ) -> None:
        self._uow = uow
        self.appointments = AppointmentService(uow, calendar)
        self.availability = AvailabilityService(uow)
        self.access = access
        self.identity = identity

    async def identify(
        self, session: Session, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        """Redeem a code reception gave the caller. Phone calls only."""
        command = IdentifyPatient.model_validate(payload.model_dump())
        identity = state.identity_state
        if session.provider_call_id is None or identity.status == VerificationStatus.FAILED:
            return Outcome(status="denied", next="offer_to_take_a_message")
        if not identity.is_verified:
            if identity.status != VerificationStatus.COLLECTING_IDENTIFIER:
                identity.start_collecting(IdentifierType.ACCESS_CODE)
            identity.status = VerificationStatus.VERIFYING
        patient_id = await self.access.identify(session.call, command.access_code)
        if patient_id is None:
            if identity.is_verified:
                state.identity_state = IdentityState()
            elif identity.status == VerificationStatus.VERIFYING:
                identity.record_attempt(False)
            return Outcome(status="denied", next="offer_to_take_a_message")
        if state.identity_state.patient_id != patient_id or not state.identity_state.is_verified:
            if identity.status != VerificationStatus.VERIFYING:
                state.identity_state = IdentityState()
            state.identity_state.record_attempt(True, patient_id)
            state.identity_state.identifier_type = IdentifierType.ACCESS_CODE
        return Outcome(status="identified")

    async def register(
        self, session: Session, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        command = RegisterInput.model_validate(payload.model_dump())
        if state.identity_state.is_verified:
            return Outcome(status="already_known")
        if not session.writes:
            return Outcome(status="test_mode", say=spoken(state.language, "test_mode"))
        if not await self.identity.register(session, state, command.full_name):
            # No number to file the person under, such as a withheld caller ID.
            return Outcome(status="cannot_register", next="offer_to_take_a_message")
        return Outcome(status="registered")

    async def slots(
        self, session: Session, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        command = AvailabilityInput.model_validate(payload.model_dump())
        query = SlotQuery(starts_at=command.starts_at, ends_at=command.ends_at)
        state.active_flow_state.transition(FlowStep.CHECKING_AVAILABILITY)
        slots = await self.availability.slots(
            session.request(state), command.provider_id, query, command.service_id
        )
        items = [
            dict(provider_id=str(command.provider_id), **slot.model_dump(mode="json"))
            for slot in slots[:20]
        ]
        state.active_flow_state.offered_slots = items
        state.active_flow_state.transition(FlowStep.OFFERING_SLOTS)
        return Outcome(status="available" if items else "no_availability", items=items)

    async def list_appointments(
        self, session: Session, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        refused = self._must_be_known(session, state, managing=True)
        if refused is not None:
            return refused
        rows = await self.appointments.list(session.request(state), 10, 0)
        return Outcome(
            status="appointments",
            items=[
                {
                    "appointment_id": str(row.id),
                    "provider_id": str(row.provider_id),
                    "starts_at": row.starts_at.isoformat(),
                    "status": row.status,
                }
                for row in rows
            ],
        )

    @staticmethod
    def _must_be_known(
        session: Session, state: ConversationState, *, managing: bool
    ) -> Outcome | None:
        """Why this person may not go on, if they may not."""
        identity = state.identity_state
        if not identity.is_verified:
            if session.contact:
                return Outcome(
                    status="identity_required", next="ask_full_name_then_register_patient"
                )
            return Outcome(status="identity_required", next="offer_to_take_a_message")
        if (
            managing
            and session.channel == "voice"
            and identity.identifier_type == IdentifierType.PHONE
            and not session.configuration.privacy.known_number_can_manage
        ):
            # A caller's number can be faked, so on a call the clinic may want more than
            # a matching number before existing appointments are shown or changed. A
            # text message is different: the answer only reaches the patient's own phone.
            return Outcome(status="staff_only", next="offer_to_take_a_message_or_ask_for_code")
        return None

    async def propose(
        self, session: Session, state: ConversationState, action: Action, payload: BaseModel
    ) -> BaseModel:
        if not session.writes:
            return Outcome(status="test_mode", say=spoken(state.language, "test_mode"))
        refused = self._must_be_known(session, state, managing=action != "book_appointment")
        if refused is not None:
            return refused
        request = session.request(state)
        params = payload.model_dump(mode="json")
        provider_id: UUID
        if action == "book_appointment":
            booking = BookInput.model_validate(payload.model_dump())
            provider_id = booking.provider_id
            # Reuse domain availability validation. A fresh slot check still occurs on commit.
            slots = await self.availability.slots(
                request,
                provider_id,
                SlotQuery(
                    starts_at=booking.starts_at, ends_at=booking.starts_at + timedelta(days=1)
                ),
                booking.service_id,
            )
            selected = next((slot for slot in slots if slot.starts_at == booking.starts_at), None)
            if selected is None:
                return Outcome(status="slot_unavailable", next="check_availability_again")
            params["patient_id"] = str(request.patient_id)
            state.appointment_draft = AppointmentDraft(
                provider_id=provider_id, start_time=selected.starts_at, end_time=selected.ends_at
            )
            state.active_flow_state.flow_type = FlowType.APPOINTMENT_BOOKING
            provider, timezone = await self._provider(session, state, provider_id)
            offer = spoken(
                state.language,
                "book_offer",
                time=when(selected.starts_at, timezone, state.language),
            )
        else:
            appointment_id = UUID(str(params["appointment_id"]))
            row = await self.appointments.get(request, appointment_id)
            if row.status != "booked" or row.starts_at <= datetime.now(UTC):
                return Outcome(status="appointment_unavailable", next="offer_to_take_a_message")
            params["expected_version"] = row.version
            provider_id = row.provider_id
            provider, timezone = await self._provider(session, state, provider_id)
            if action == "cancel_appointment":
                state.active_flow_state.flow_type = FlowType.APPOINTMENT_CANCEL
                offer = spoken(
                    state.language,
                    "cancel_offer",
                    time=when(row.starts_at, timezone, state.language),
                )
            else:
                change = RescheduleInput.model_validate(payload.model_dump())
                slots = await self.availability.slots(
                    request,
                    provider_id,
                    SlotQuery(
                        starts_at=change.starts_at, ends_at=change.starts_at + timedelta(days=1)
                    ),
                    row.service_id,
                )
                if not any(slot.starts_at == change.starts_at for slot in slots):
                    return Outcome(status="slot_unavailable", next="check_availability_again")
                state.active_flow_state.flow_type = FlowType.APPOINTMENT_RESCHEDULE
                offer = spoken(
                    state.language,
                    "move_offer",
                    old=when(row.starts_at, timezone, state.language),
                    new=when(change.starts_at, timezone, state.language),
                )
        # The actual provider name; nobody is asked to confirm an opaque ID.
        summary = spoken(state.language, "confirm", provider=provider, offer=offer)
        state.appointment_operation = AppointmentOperation(
            action=action, params=params, key=str(uuid4()), summary=summary
        )
        state.confirmation_state.request(
            action, params, summary, state.confirmation_timeout_seconds
        )
        state.active_flow_state.transition(FlowStep.AWAITING_CONFIRMATION)
        state.active_intent = action
        return Outcome(status="confirmation_required", say=summary)

    async def _provider(
        self, session: Session, state: ConversationState, provider_id: UUID
    ) -> tuple[str, str]:
        async with self._uow.transaction(session.request(state)) as tx:
            name = (await tx.providers.display_names({provider_id})).get(provider_id, "")
            return name, await tx.schedule.timezone(provider_id) or "UTC"

    async def before_turn(
        self, session: Session, state: ConversationState, utterance: str
    ) -> Direct | None:
        """Answer without the model: a reception code, or yes/no to a pending question."""
        code = normalize_access_code(utterance.strip())
        if (
            re.fullmatch(r"[0-9]{8}", code)
            and session.provider_call_id is not None
            and "identify_patient" in session.allowed_tools
        ):
            result = Outcome.model_validate(
                (
                    await self.identify(session, state, IdentifyPatient(access_code=code))
                ).model_dump()
            )
            state.last_caller_utterance = "[call access code]"
            return Direct(
                spoken(
                    state.language,
                    "identified" if result.status == "identified" else "access_denied",
                ),
                "identify_patient",
            )
        operation = state.appointment_operation
        if operation is not None and operation.action not in session.allowed_tools:
            raise AssistantAuthorizationError("Appointment action is not authorized")
        if operation is None or operation.status == "completed":
            return None
        confirmation = state.confirmation_state
        if operation.status != "executing":
            valid = (
                confirmation.status
                in {ConfirmationStatus.AWAITING_CONFIRMATION, ConfirmationStatus.CONFIRMED}
                and confirmation.expires_at is not None
                and confirmation.expires_at > datetime.now(UTC)
                and confirmation.action == operation.action
                and confirmation.action_params == operation.params
            )
            if not valid or confirmation_answer(utterance, state.language) is not True:
                # Revisions and interruptions discard the old proposal, even after a previous yes.
                state.appointment_operation = None
                confirmation.reset()
                state.active_flow_state.transition(FlowStep.COLLECTING_DETAILS)
                if confirmation_answer(utterance, state.language) is False:
                    return Direct(spoken(state.language, "discarded"), operation.action)
                return None
            operation.status = "executing"
            state.active_flow_state.transition(FlowStep.EXECUTING)
        # Resuming an interrupted operation uses exactly the same server key and arguments.
        request = session.request(state)
        params = operation.params.copy()
        try:
            if operation.action == "book_appointment":
                result = await self.appointments.book(
                    request, BookAppointment.model_validate(params), operation.key
                )
                key = "booked"
            elif operation.action == "cancel_appointment":
                identity = UUID(str(params.pop("appointment_id")))
                result = await self.appointments.cancel(
                    request, identity, CancelAppointment.model_validate(params), operation.key
                )
                key = "cancelled"
            else:
                identity = UUID(str(params.pop("appointment_id")))
                result = await self.appointments.reschedule(
                    request, identity, RescheduleAppointment.model_validate(params), operation.key
                )
                key = "moved"
            _, timezone = await self._provider(session, state, result.provider_id)
            message = spoken(
                state.language, key, time=when(result.starts_at, timezone, state.language)
            )
        except ApplicationError as error:
            operation.status = "completed"
            confirmation.reset()
            state.active_flow_state.transition(FlowStep.FAILED)
            localized = {
                "slot_unavailable": "slot_unavailable",
                "past_time": "slot_unavailable",
                "stale_appointment": "appointment_unavailable",
                "appointment_state_conflict": "appointment_unavailable",
                "too_late_to_change": "appointment_unavailable",
            }.get(error.code, "unavailable")
            message = (
                error.public_message
                if state.language == "en"
                else spoken(state.language, localized)
            )
            return Direct(message, operation.action)
        operation.status = "completed"
        confirmation.reset()
        state.active_flow_state.transition(FlowStep.COMPLETED)
        ConversationStateManager.record_tool_call(state, operation.action)
        return Direct(message, operation.action)
