"""Remembering the whole call, and trusted callers across calls."""

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.ai.llm.base import LLMRequest
from app.ai.memory import Summary, records_trusted
from app.ai.prompts import HISTORY_TURNS, history, system_prompt
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState, IdentifierType
from app.postcall.schemas import PostCallInput, analyze_post_call
from app.settings.defaults import default_settings
from tests.test_voice_latency import ScriptedEngine, receptionist_for


def new_state() -> ConversationState:
    return ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA" + "0" * 32)


def test_the_model_keeps_contact_details_the_saved_transcript_does_not() -> None:
    state = new_state()
    state.append_transcript_turn("caller", "My email is asha@example.com, number 9876543210.")
    turn = state.transcript_turns[-1]
    assert "asha@example.com" not in turn.text and "[email]" in turn.text
    assert turn.model_text is not None and "asha@example.com" in turn.model_text
    assert "asha@example.com" in history(state, "x")[-1].text  # type: ignore[union-attr]
    # Neither the store nor the post-call snapshot ever gets the unredacted form.
    assert "asha@example.com" not in state.model_dump_json()
    assert "asha@example.com" not in PostCallInput.capture(state).model_dump_json()


def test_access_codes_stay_hidden_from_the_model() -> None:
    state = new_state()
    state.append_transcript_turn("caller", "My code is 12345678")
    assert "12345678" not in (state.transcript_turns[-1].model_text or "")
    assert "12345678" not in state.transcript_turns[-1].text


def test_a_long_call_is_kept_and_only_the_last_80_turns_are_saved() -> None:
    state = new_state()
    for number in range(150):
        state.append_transcript_turn("caller" if number % 2 else "agent", f"Turn {number}")
    assert len(state.transcript_turns) == 150 and state.turns_appended == 150
    snapshot = PostCallInput.capture(state, "Asked to move Monday's visit; moved to Tuesday.")
    assert len(snapshot.turns) == 80 and snapshot.turns[-1].text == "Turn 149"
    analysis = analyze_post_call("completed", snapshot)
    assert analysis.summary == "Asked to move Monday's visit; moved to Tuesday."


def test_lookups_are_remembered_briefly() -> None:
    state = new_state()
    for number in range(5):
        state.record_lookup("check_availability", {"items": ["x" * 400], "n": number})
    assert len(state.recent_lookups) == 3
    assert all(len(item) <= len("check_availability: ") + 300 for item in state.recent_lookups)


def session_for(channel: str = "voice", **privacy: Any) -> Any:
    configuration = default_settings("Lotus Clinic")
    configuration = configuration.model_copy(
        update={"privacy": configuration.privacy.model_copy(update=privacy)}
    )
    return MagicMock(configuration=configuration, channel=channel, contact="+919876543210")


def verified(state: ConversationState, by: IdentifierType) -> ConversationState:
    state.identity_state.record_attempt(True, uuid4())
    state.identity_state.identifier_type = by
    return state


def test_records_are_trusted_only_when_the_caller_is_really_known() -> None:
    assert not records_trusted(session_for(), new_state())
    by_number = verified(new_state(), IdentifierType.PHONE)
    # A caller's number can be faked...
    assert not records_trusted(session_for(), by_number)
    # ...unless the clinic trusts it, and a text only ever reaches that phone.
    assert records_trusted(session_for(known_number_can_manage=True), by_number)
    assert records_trusted(session_for("sms"), by_number)
    by_code = verified(new_state(), IdentifierType.ACCESS_CODE)
    assert records_trusted(session_for(), by_code)


def test_history_reaches_the_prompt_only_for_a_trusted_caller() -> None:
    state = verified(new_state(), IdentifierType.PHONE)
    state.caller_history = "Coming appointment: Monday 14 Oct at 10:30 with Dr. Rao"
    assert "Dr. Rao" not in system_prompt(session_for(), state)
    assert "Dr. Rao" in system_prompt(session_for(known_number_can_manage=True), state)


class Summarizer:
    model = "light"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    async def generate_structured[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> T:
        self.requests.append(request)
        return schema.model_validate(Summary(summary="Asha wants Dr. Rao on Monday.").model_dump())


@pytest.mark.asyncio
async def test_turns_out_of_view_are_summarized_in_the_background() -> None:
    receptionist = receptionist_for(ScriptedEngine([]))
    summarizer = Summarizer()
    receptionist.summarizer = summarizer  # type: ignore[assignment]
    for number in range(HISTORY_TURNS + 12):
        receptionist.state.append_transcript_turn(
            "caller" if number % 2 else "agent", f"Turn {number}"
        )
    receptionist._keep_summary()  # pyright: ignore[reportPrivateUsage]
    assert receptionist._summarizing is not None  # pyright: ignore[reportPrivateUsage]
    await receptionist._summarizing  # pyright: ignore[reportPrivateUsage]
    state = receptionist.state
    assert state.earlier_summary == "Asha wants Dr. Rao on Monday."
    assert state.summarized_through == 12
    # Only the turns the model no longer sees were sent to be summarized.
    payload = summarizer.requests[0].messages[-1].content
    assert "Turn 11" in payload and "Turn 12" not in payload
    assert "Asha wants Dr. Rao on Monday." in system_prompt(session_for(), state)


@pytest.mark.asyncio
async def test_the_call_summary_is_written_by_the_model_or_left_to_the_rules() -> None:
    receptionist = receptionist_for(ScriptedEngine([]))
    receptionist.state.append_transcript_turn("caller", "Can I move my visit?")
    receptionist.state.append_transcript_turn("agent", "Sure, I've moved it to Tuesday.")
    assert await receptionist.call_summary() is None
    receptionist.summarizer = Summarizer()  # type: ignore[assignment]
    assert await receptionist.call_summary() == "Asha wants Dr. Rao on Monday."
