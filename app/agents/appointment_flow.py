"""Deterministic appointment proposals and confirmation, independent of model decisions."""

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.agents.context import AgentAuthorizationError, AgentContext
from app.agents.language import spoken
from app.agents.schemas import AgentResult, Intent
from app.appointments.availability import AvailabilityService
from app.appointments.commands import (
    BookAppointment,
    CancelAppointment,
    RescheduleAppointment,
    SlotQuery,
)
from app.appointments.service import AppointmentService
from app.auth.context import ActorType, RequestContext
from app.calls.access import CallPatientAccess, IdentifyPatient
from app.calls.directory import CallDirectoryService, DirectoryQuery
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
from app.database.unit_of_work import UnitOfWork
from app.errors import ApplicationError


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AvailabilityInput(ToolInput):
    provider_id: UUID
    starts_at: AwareDatetime
    ends_at: AwareDatetime


class BookInput(ToolInput):
    provider_id: UUID
    starts_at: AwareDatetime


class CancelInput(ToolInput):
    appointment_id: UUID


class RescheduleInput(CancelInput):
    starts_at: AwareDatetime


class AppointmentOutput(BaseModel):
    status: str
    message: str = ""
    items: list[dict[str, Any]] = Field(default_factory=lambda: list[dict[str, Any]]())


def caller_context(context: AgentContext, state: ConversationState) -> RequestContext:
    context.validate(state)
    return RequestContext(
        context.call.request_id,
        context.call.tenant_id,
        None,
        ActorType.CALLER,
        call_id=context.call.call_id,
        provider_call_id=context.call.provider_call_id,
        patient_id=state.identity_state.patient_id if state.identity_state.is_verified else None,
    )


def affirmative(text: str, language: str = "en") -> bool:
    return confirmation_answer(text, language) is True


class AppointmentFlow:
    def __init__(self, uow: UnitOfWork, access: CallPatientAccess) -> None:
        self.appointments = AppointmentService(uow)
        self.availability = AvailabilityService(uow)
        self.access = access

    async def identify(
        self, context: AgentContext, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        command = IdentifyPatient.model_validate(payload.model_dump())
        identity = state.identity_state
        if identity.status == VerificationStatus.FAILED:
            return AppointmentOutput(
                status="denied", message=spoken(state.language, "access_denied")
            )
        if not identity.is_verified:
            if identity.status != VerificationStatus.COLLECTING_IDENTIFIER:
                identity.start_collecting(IdentifierType.ACCESS_CODE)
            identity.status = VerificationStatus.VERIFYING
        patient_id = await self.access.identify(context.call, command.access_code)
        if patient_id is None:
            if identity.is_verified:
                state.identity_state = IdentityState()
            elif identity.status == VerificationStatus.VERIFYING:
                identity.record_attempt(False)
            return AppointmentOutput(
                status="denied", message=spoken(state.language, "access_denied")
            )
        if state.identity_state.patient_id != patient_id or not state.identity_state.is_verified:
            if identity.status != VerificationStatus.VERIFYING:
                state.identity_state = IdentityState()
            state.identity_state.record_attempt(True, patient_id)
        return AppointmentOutput(status="identified", message=spoken(state.language, "identified"))

    async def slots(
        self, context: AgentContext, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        command = AvailabilityInput.model_validate(payload.model_dump())
        query = SlotQuery(starts_at=command.starts_at, ends_at=command.ends_at)
        state.active_flow_state.transition(FlowStep.CHECKING_AVAILABILITY)
        slots = await self.availability.slots(
            caller_context(context, state), command.provider_id, query
        )
        items = [
            dict(provider_id=str(command.provider_id), **slot.model_dump(mode="json"))
            for slot in slots[:20]
        ]
        state.active_flow_state.offered_slots = items
        state.active_flow_state.transition(FlowStep.OFFERING_SLOTS)
        return AppointmentOutput(status="available" if items else "no_availability", items=items)

    async def list_appointments(
        self, context: AgentContext, state: ConversationState, payload: BaseModel
    ) -> BaseModel:
        query = DirectoryQuery.model_validate(payload.model_dump())
        rows = await self.appointments.list(
            caller_context(context, state), query.limit, query.offset
        )
        return AppointmentOutput(
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

    async def propose(
        self,
        context: AgentContext,
        state: ConversationState,
        action: Literal["book_appointment", "cancel_appointment", "reschedule_appointment"],
        payload: BaseModel,
    ) -> BaseModel:
        request = caller_context(context, state)
        if request.patient_id is None:
            return AppointmentOutput(
                status="identity_required",
                message=spoken(state.language, "identity_required"),
            )
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
            )
            selected = next((slot for slot in slots if slot.starts_at == booking.starts_at), None)
            if selected is None:
                return AppointmentOutput(
                    status="unavailable",
                    message=spoken(state.language, "slot_unavailable"),
                )
            params["patient_id"] = str(request.patient_id)
            state.appointment_draft = AppointmentDraft(
                provider_id=provider_id, start_time=selected.starts_at, end_time=selected.ends_at
            )
            state.active_flow_state.flow_type = FlowType.APPOINTMENT_BOOKING
            summary = spoken(state.language, "book_offer", time=selected.starts_at.isoformat())
        else:
            appointment_id = UUID(str(params["appointment_id"]))
            row = await self.appointments.get(request, appointment_id)
            if row.status != "booked" or row.starts_at <= datetime.now(UTC):
                return AppointmentOutput(
                    status="unavailable",
                    message=spoken(state.language, "appointment_unavailable"),
                )
            params["expected_version"] = row.version
            provider_id = row.provider_id
            if action == "cancel_appointment":
                state.active_flow_state.flow_type = FlowType.APPOINTMENT_CANCEL
                summary = spoken(state.language, "cancel_offer", time=row.starts_at.isoformat())
            else:
                change = RescheduleInput.model_validate(payload.model_dump())
                slots = await self.availability.slots(
                    request,
                    provider_id,
                    SlotQuery(
                        starts_at=change.starts_at, ends_at=change.starts_at + timedelta(days=1)
                    ),
                )
                if not any(slot.starts_at == change.starts_at for slot in slots):
                    return AppointmentOutput(
                        status="unavailable",
                        message=spoken(state.language, "new_slot_unavailable"),
                    )
                state.active_flow_state.flow_type = FlowType.APPOINTMENT_RESCHEDULE
                summary = spoken(
                    state.language,
                    "move_offer",
                    old=row.starts_at.isoformat(),
                    new=change.starts_at.isoformat(),
                )
        # Display the actual provider name and timezone; never ask confirmation of an opaque UUID.
        directory = await CallDirectoryService(self.access.sessions).provider_label(
            context.call, provider_id
        )
        summary = spoken(state.language, "confirm", provider=directory, offer=summary)
        state.appointment_operation = AppointmentOperation(
            action=action, params=params, key=str(uuid4()), summary=summary
        )
        state.confirmation_state.request(
            action, params, summary, state.confirmation_timeout_seconds
        )
        state.active_flow_state.transition(FlowStep.AWAITING_CONFIRMATION)
        state.active_intent = action
        return AppointmentOutput(status="confirmation_required", message=summary)

    async def before_turn(
        self, context: AgentContext, state: ConversationState, utterance: str
    ) -> AgentResult | None:
        code = utterance.strip()
        if re.fullmatch(r"[0-9]{8}", code) and "identify_patient" in context.allowed_tools:
            result = await self.identify(context, state, IdentifyPatient(access_code=code))
            output = AppointmentOutput.model_validate(result.model_dump())
            state.last_caller_utterance = "[call access code]"
            ConversationStateManager.record_agent_response(state, output.message)
            return AgentResult(
                intent=Intent.UNKNOWN, response=output.message, tools_used=["identify_patient"]
            )
        operation = state.appointment_operation
        if operation is not None and operation.action not in context.allowed_tools:
            raise AgentAuthorizationError("Appointment action is not authorized")
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
            if not valid or not affirmative(utterance, state.language):
                # Revisions and interruptions discard the old proposal, even after a previous yes.
                state.appointment_operation = None
                confirmation.reset()
                state.active_flow_state.transition(FlowStep.COLLECTING_DETAILS)
                if confirmation_answer(utterance, state.language) is False:
                    return self._reply(
                        state,
                        operation.action,
                        spoken(state.language, "discarded"),
                    )
                return None
            operation.status = "executing"
            state.active_flow_state.transition(FlowStep.EXECUTING)
        # Resuming an interrupted operation uses exactly the same server key and arguments.
        request = caller_context(context, state)
        params = operation.params.copy()
        try:
            if operation.action == "book_appointment":
                result = await self.appointments.book(
                    request, BookAppointment.model_validate(params), operation.key
                )
                message = spoken(state.language, "booked", time=result.starts_at.isoformat())
            elif operation.action == "cancel_appointment":
                identity = UUID(str(params.pop("appointment_id")))
                result = await self.appointments.cancel(
                    request, identity, CancelAppointment.model_validate(params), operation.key
                )
                message = spoken(state.language, "cancelled")
            else:
                identity = UUID(str(params.pop("appointment_id")))
                result = await self.appointments.reschedule(
                    request, identity, RescheduleAppointment.model_validate(params), operation.key
                )
                message = spoken(state.language, "moved", time=result.starts_at.isoformat())
        except ApplicationError as error:
            operation.status = "completed"
            confirmation.reset()
            state.active_flow_state.transition(FlowStep.FAILED)
            localized = {
                "slot_unavailable": "slot_unavailable",
                "past_time": "slot_unavailable",
                "stale_appointment": "appointment_unavailable",
                "appointment_state_conflict": "appointment_unavailable",
            }.get(error.code, "unavailable")
            message = (
                error.public_message
                if state.language == "en"
                else spoken(state.language, localized)
            )
            return self._reply(state, operation.action, message)
        operation.status = "completed"
        confirmation.reset()
        state.active_flow_state.transition(FlowStep.COMPLETED)
        return self._reply(state, operation.action, message)

    @staticmethod
    def _reply(state: ConversationState, action: str, message: str) -> AgentResult:
        ConversationStateManager.record_agent_response(state, message)
        return AgentResult(intent=Intent(action), response=message, tools_used=[action])
