"""Rescheduling by the calling number or the appointment number: the check a caller
passes before their appointments are shown or changed. No database; the identity
lookup is a stand-in."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.ai.caller_check import appointment_number, names_match
from app.ai.memory import records_trusted
from app.ai.tools.appointments import AppointmentFlow, ConfirmInput, RegisterInput
from app.ai.wording import spoken
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, IdentifierType
from app.settings.defaults import default_settings


def session_for(**privacy: Any) -> Any:
    configuration = default_settings("Lotus Clinic")
    configuration = configuration.model_copy(
        update={"privacy": configuration.privacy.model_copy(update=privacy)}
    )
    return MagicMock(
        configuration=configuration,
        channel="voice",
        contact="+919876543210",
        provider_call_id="CA" + "1" * 32,
    )


def state_for(by: IdentifierType | None = None) -> ConversationState:
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA" + "0" * 32)
    if by is not None:
        state.identity_state.record_attempt(True, uuid4())
        state.identity_state.identifier_type = by
    return state


def flow_with(found: tuple[Any, str] | None) -> tuple[AppointmentFlow, AsyncMock]:
    flow = AppointmentFlow.__new__(AppointmentFlow)
    lookup = AsyncMock(return_value=found)
    flow.identity = MagicMock(confirm=lookup)
    return flow, lookup


def test_names_and_numbers_are_understood_the_way_they_are_said() -> None:
    assert names_match("Nimith", "Nimit Desai")  # misheard
    assert names_match("Mr Nimit Desai", "Nimit Desai")
    assert not names_match("Rahul", "Nimit Desai")
    assert not names_match("Nimit Shah", "Nimit Desai")
    assert [appointment_number(said) for said in ("A-3", "a three", "A 10 42", "none")] == [
        3,
        3,
        1042,
        None,
    ]


def test_a_caller_on_their_own_number_is_asked_for_the_name_not_a_code() -> None:
    refused = AppointmentFlow._must_be_known(  # pyright: ignore[reportPrivateUsage]
        session_for(), state_for(IdentifierType.PHONE), managing=True
    )
    assert refused is not None and refused.status == "confirm_identity"
    assert "name_on_the_appointment" in refused.next


def test_a_caller_from_another_number_is_asked_for_the_appointment_number_too() -> None:
    refused = AppointmentFlow._must_be_known(  # pyright: ignore[reportPrivateUsage]
        session_for(), state_for(), managing=True
    )
    assert refused is not None and "appointment_number_and_name" in refused.next
    # Booking a new visit still only needs the name.
    booking = AppointmentFlow._must_be_known(  # pyright: ignore[reportPrivateUsage]
        session_for(), state_for(), managing=False
    )
    assert booking is not None and booking.next == "ask_full_name_then_register_patient"


def test_a_clinic_can_keep_the_code_from_reception_instead() -> None:
    refused = AppointmentFlow._must_be_known(  # pyright: ignore[reportPrivateUsage]
        session_for(confirm_callers_by_name=False), state_for(IdentifierType.PHONE), managing=True
    )
    assert refused is not None and refused.status == "staff_only"


@pytest.mark.asyncio
async def test_the_right_name_lets_them_manage_their_appointments() -> None:
    patient = uuid4()
    flow, lookup = flow_with((patient, "Nimit Desai"))
    session, state = session_for(), state_for(IdentifierType.PHONE)
    result = await flow.confirm(session, state, ConfirmInput(full_name="Nimit"))
    assert result.status == "confirmed"  # type: ignore[attr-defined]
    assert lookup.call_args.args[2:] == ("Nimit", None)
    assert state.identity_state.identifier_type == IdentifierType.CONFIRMED
    assert state.identity_state.patient_id == patient and state.caller_name == "Nimit"
    assert records_trusted(session, state)
    assert (
        AppointmentFlow._must_be_known(session, state, managing=True)  # pyright: ignore[reportPrivateUsage]
        is None
    )


@pytest.mark.asyncio
async def test_from_another_phone_the_appointment_number_is_needed() -> None:
    flow, lookup = flow_with((uuid4(), "Nimit Desai"))
    state = state_for()
    asked = await flow.confirm(session_for(), state, ConfirmInput(full_name="Nimit"))
    assert asked.status == "appointment_number_needed"  # type: ignore[attr-defined]
    lookup.assert_not_awaited()
    done = await flow.confirm(
        session_for(), state, ConfirmInput(full_name="Nimit", appointment_number="A-3")
    )
    assert done.status == "confirmed"  # type: ignore[attr-defined]
    assert lookup.call_args.args[3] == 3


@pytest.mark.asyncio
async def test_wrong_details_get_a_few_tries_and_say_nothing_about_whose_it_is() -> None:
    flow, _ = flow_with(None)
    session, state = session_for(), state_for(IdentifierType.PHONE)
    outcomes = [
        await flow.confirm(session, state, ConfirmInput(full_name="Rahul")) for _ in range(4)
    ]
    assert [item.status for item in outcomes] == [  # type: ignore[attr-defined]
        "not_matched",
        "not_matched",
        "not_matched",
        "too_many_tries",
    ]
    assert outcomes[2].next == "offer_to_take_a_message"  # type: ignore[attr-defined]
    assert all(not item.items for item in outcomes)  # type: ignore[attr-defined]
    # Still known by their number for booking, just not for changing appointments.
    assert state.identity_state.identifier_type == IdentifierType.PHONE


def test_a_name_said_in_english_matches_one_stored_in_hindi_or_spelled_out() -> None:
    # 27 Oct: "Vimit Desai", then "N I m I p. B e s a I", never matched "निमित देसाई".
    assert names_match("Vimit Desai", "निमित देसाई")
    assert names_match("N I m I p. B e s a I", "निमित देसाई")
    assert names_match("Nimit Desai", "નિમિત દેસાઈ")
    assert names_match("N I M I T D E S A I", "Nimit Desai")
    assert names_match("Bhavesh Shah", "भावेश शाह") and names_match("José", "Jose Silva")
    assert not names_match("Rahul Shah", "निमित देसाई")


def new_caller() -> tuple[AppointmentFlow, AsyncMock, Any, ConversationState]:
    flow = AppointmentFlow.__new__(AppointmentFlow)
    register = AsyncMock(return_value=True)
    flow.identity = MagicMock(register=register)
    session = session_for()
    session.writes = True
    return flow, register, session, state_for()


@pytest.mark.asyncio
async def test_on_a_call_a_new_name_is_read_back_and_registered_on_a_yes() -> None:
    flow, register, session, state = new_caller()
    heard = await flow.register(session, state, RegisterInput(full_name="Kit Balsar"))
    assert heard.status == "name_read_back" and "Kit Balsar" in heard.say  # type: ignore[attr-defined]
    register.assert_not_awaited()
    # A yes registers it, and the model carries on with the booking.
    assert await flow.before_turn(session, state, "Yes, that's right") is None
    assert register.call_args.args[2] == "Kit Balsar"
    assert state.caller_name == "Kit" and state.identity_state.pending_name is None


@pytest.mark.asyncio
async def test_a_wrong_name_is_asked_for_another_way_then_kept_for_the_team() -> None:
    flow, register, session, state = new_caller()
    asked: list[str] = []
    for heard in ("With browser", "Kit Bausar", "Kit Balsar"):
        await flow.register(session, state, RegisterInput(full_name=heard))
        reply = await flow.before_turn(session, state, "No, that's wrong")
        assert reply is not None
        asked.append(reply.response)
    assert asked == [
        spoken("en", "name_spell"),
        spoken("en", "name_slowly"),
        spoken("en", "name_noted"),
    ]
    assert register.await_count == 1 and register.call_args.kwargs == {"unsure": True}
    # A correction goes back to the model, which registers the name again.
    flow, register, session, state = new_caller()
    await flow.register(session, state, RegisterInput(full_name="Kit Bausar"))
    assert await flow.before_turn(session, state, "No, it's Balsar. B A L S A R") is None
    register.assert_not_awaited()
