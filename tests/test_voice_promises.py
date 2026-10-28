"""Saying "let me check" means checking, and callers are called by their name.

The phrases are from a Flash-Lite check where the receptionist said it would look and
then stopped, leaving the caller in silence.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.ai.engine import KEEP_PROMISE
from app.ai.llm.base import LLMInvalidResponseError, UserTurn
from app.ai.llm.factory import open_models
from app.ai.memory import first_name
from app.ai.prompts import system_prompt
from app.ai.speech_time import spoken_when
from app.ai.tools.appointments import AppointmentFlow, AvailabilityInput
from app.appointments.schemas import Slot
from app.conversation.promises import promises_a_lookup
from tests.test_voice_transfer import call, clinic, turn, voice_engine


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("नमस्ते जी, मैं अभी Dr. Nimiy की कल की खाली जगह देख लेती हूँ।", True),
        ("ડૉ. નિમિય પાસે કાલે સવારે શું સમય ખાલી છે તે હું જોઈ લઉં છું.", True),
        ("Let me check Dr. Nimiy's diary for tomorrow evening.", True),
        ("Ji, main abhi dekhti hoon.", True),
        ("Déjeme ver la agenda.", True),
        ("Sure, I'll take a look.", True),
        ("We open at nine tomorrow.", False),
        ("કોઈ સમય જોઈએ છે?", False),  # "do you need a time?"
        ("कल शाम पाँच बजे खाली है, ठीक रहेगा?", False),
        ("Thank you for calling, have a lovely day!", False),
    ],
)
def test_a_promised_lookup_is_noticed(reply: str, expected: bool) -> None:
    assert promises_a_lookup(reply) is expected


async def test_saying_let_me_check_and_stopping_gets_a_reminder() -> None:
    built = voice_engine("Let me check what Friday looks like.", "Friday is free at ten.")
    session, state = call(clinic())
    said, _, done = await turn(built, session, state, "Is Friday free?")
    assert said == ["Let me check what Friday looks like.", "Friday is free at ten."]
    reminded = built.model.requests[1].turns[-1]  # type: ignore[attr-defined]
    assert reminded == UserTurn(KEEP_PROMISE)
    assert done.outcome == "answered"


async def test_a_reminder_with_nothing_to_look_up_lets_the_reply_stand() -> None:
    built = voice_engine(
        "Let me check that.", LLMInvalidResponseError("LLM returned no usable response")
    )
    session, state = call(clinic())
    said, _, done = await turn(built, session, state, "Is Friday free?")
    # No "sorry, I could not answer" after it: what was said stands.
    assert said == ["Let me check that."]
    assert done.outcome == "answered"


async def test_a_reply_that_asks_the_caller_something_waits_for_them() -> None:
    built = voice_engine("Sure, let me check. May I have your name, please?")
    session, state = call(clinic())
    said, _, _ = await turn(built, session, state, "Is Friday free?")
    assert said == ["Sure, let me check.", "May I have your name, please?"]
    assert len(built.model.requests) == 1  # type: ignore[attr-defined]


async def test_an_answer_without_a_promise_is_not_questioned() -> None:
    built = voice_engine("We open at nine tomorrow.")
    session, state = call(clinic())
    said, _, _ = await turn(built, session, state, "When do you open?")
    assert said == ["We open at nine tomorrow."]
    assert len(built.model.requests) == 1  # type: ignore[attr-defined]


def test_the_model_knows_the_callers_name_and_asks_for_it_when_it_does_not() -> None:
    session, state = call(clinic())
    prompt = system_prompt(session, state)
    assert '"their_name": null' in prompt and "Can I get your name" in prompt
    state.caller_name = "Nimit"
    assert '"their_name": "Nimit"' in system_prompt(session, state)


def test_hinglish_callers_get_hinglish_back() -> None:
    session, state = call(clinic())
    state.language_state.primary_language = "hinglish"
    prompt = system_prompt(session, state)
    # The language rule comes last, so nothing before it outweighs it.
    assert prompt.rstrip().endswith("say them in Hinglish).")
    assert "reply only in Hinglish" in prompt and "Never plain English" in prompt


@pytest.mark.parametrize(
    ("name", "expected"),
    [("निमित देसाई", "निमित"), ("Meera Shah", "Meera"), ("  ", None), ("Ravi", "Ravi")],
)
def test_callers_are_called_by_their_first_name(name: str, expected: str | None) -> None:
    assert first_name(name) == expected


async def test_free_times_come_already_said() -> None:
    session, state = call(clinic())
    state.language_state.primary_language = "hi"
    start = datetime(2026, 10, 27, 12, 45, tzinfo=UTC)
    flow = AppointmentFlow.__new__(AppointmentFlow)
    flow.availability = MagicMock()
    flow.availability.slots = AsyncMock(return_value=[Slot(starts_at=start, ends_at=start)])
    asked = AvailabilityInput(
        provider_id=uuid4(), starts_at=start, ends_at=start + timedelta(hours=1)
    )
    item = (await flow.slots(session, state, asked)).model_dump()["items"][0]
    timezone = session.configuration.clinic.timezone
    # The model repeats this; it never works the clock out itself.
    assert item["when"] == spoken_when(start, timezone, "hi")
    assert "starts_at" in item


def test_calls_are_answered_by_flash_lite_and_chat_keeps_the_main_model() -> None:
    key, config = SecretStr("test-key"), {"model": "google/gemini-3.8-flash"}
    voice = open_models("openrouter", key, config, "platform", voice=True).reply
    chat = open_models("openrouter", key, config, "platform", voice=False).reply
    assert voice.model == "google/gemini-3.5-flash-lite"  # type: ignore[attr-defined]
    assert chat.model == "google/gemini-3.8-flash"  # type: ignore[attr-defined]
