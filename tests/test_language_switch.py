"""The call keeps its language until the caller changes it. From the 27 Oct calls:
18:13 (Hindi lines labelled English), 18:38 (an English line in a Hindi call) and
18:46 (English labelled Hindi; a time "kept" that was never booked)."""

from uuid import uuid4

import pytest

from app.ai.engine import BOOK_IT, RIGHT_LANGUAGE
from app.ai.llm.base import UserTurn
from app.conversation.language import asked_for_language, reply_fits
from app.conversation.manager import ConversationStateManager
from app.conversation.promises import settles_a_time
from app.conversation.state import ConversationState
from tests.test_voice_transfer import call, clinic, turn, voice_engine

ENABLED = ("en", "hi", "gu", "es")


def fresh() -> ConversationState:
    return ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA" + "0" * 32)


def say(state: ConversationState, text: str, label: str | None = None) -> None:
    ConversationStateManager.process_caller_utterance(state, text, detected_language=label)


def test_one_misheard_english_line_does_not_switch_a_hindi_call() -> None:
    state = fresh()
    say(state, "हमारे appointment का timing change करवाना है", "hi")
    assert state.language == "hi"
    say(state, "That's what I get now.", "en")
    say(state, "दस बजे का चाहिए.", "en")
    say(state, "Mares मुझे साढ़े दस की appointment चाहिए.", "en")
    assert state.language == "hi"


def test_english_labelled_hindi_by_the_recogniser_stays_english() -> None:
    state = fresh()
    say(state, "I want to book an appointment for dental.", "hi")
    assert state.language == "en"
    say(state, "Okay.", "hi")
    say(state, "What times do you have tomorrow?", "hi")
    assert state.language == "en"


def test_the_caller_switching_or_asking_is_followed() -> None:
    state = fresh()
    say(state, "मुझे कल का appointment चाहिए")
    say(state, "I would like tomorrow morning please.")
    assert state.language == "hi"  # one sentence is not a switch
    say(state, "Is ten o'clock free tomorrow?")
    assert state.language == "en"  # two in a row are
    say(state, "हिंदी में बात कीजिए")
    assert state.language == "hi"  # asked: at once


def test_hindi_and_hinglish_are_one_language_for_a_call() -> None:
    state = fresh()
    say(state, "Mujhe kal ka appointment chahiye")
    assert state.language == "hinglish"
    say(state, "मुझे सुबह का समय चाहिए")
    say(state, "दस बजे का ठीक रहेगा")
    assert state.language == "hinglish"


@pytest.mark.parametrize(
    ("said", "language"),
    [
        ("Can we speak in English please?", "en"),
        ("English please", "en"),
        ("Hindi mein baat karo", "hi"),
        ("हिंदी में बोलिए", "hi"),
        ("ગુજરાતી માં વાત કરો", "gu"),
        ("My appointment is about my English test results", None),
        ("Hindi or English, either is fine", None),
        ("Can I speak French?", None),  # not offered by this clinic
    ],
)
def test_asking_for_a_language(said: str, language: str | None) -> None:
    assert asked_for_language(said, ENABLED) == language


@pytest.mark.parametrize(
    ("reply", "language", "fits"),
    [
        ("Sure, what's the name on the appointment?", "hi", False),
        ("जी, appointment पर जो नाम है वो बता दीजिए?", "hi", True),
        ("Ji, kal subah dus baje khaali hai.", "hi", True),
        ("Hum dental services offer nahi karte hain.", "en", False),
        ("Okay, Cheet ji.", "en", True),  # too short to tell
        ("We have nine or nine fifteen tomorrow.", "en", True),
    ],
)
def test_a_reply_is_checked_against_the_call_language(
    reply: str, language: str, fits: bool
) -> None:
    assert reply_fits(reply, language) is fits


async def test_an_english_reply_in_a_hindi_call_is_not_said_but_asked_again() -> None:
    built = voice_engine(
        "Sure, what's the name on the appointment?", "जी, appointment पर नाम क्या है?"
    )
    session, state = call(clinic())
    state.language_state.primary_language = "hi"
    said, _, done = await turn(built, session, state, "मेरी appointment कौन सी है?")
    assert said == ["जी, appointment पर नाम क्या है?"]
    asked = built.model.requests[1].turns[-1]  # type: ignore[attr-defined]
    assert asked == UserTurn(RIGHT_LANGUAGE.format(language="Hindi"))
    assert done.outcome == "answered"


async def test_asked_again_once_only() -> None:
    built = voice_engine("Sure, what's the name please?", "And your name, please?")
    session, state = call(clinic())
    state.language_state.primary_language = "hi"
    said, _, _ = await turn(built, session, state, "मेरी appointment कौन सी है?")
    assert said == ["And your name, please?"]  # better said than silence
    assert len(built.model.requests) == 2  # type: ignore[attr-defined]


OFFERED = [{"when": "आज सुबह नौ बजे"}, {"when": "आज सुबह सवा नौ बजे"}]


def test_a_time_spoken_of_as_settled_is_noticed() -> None:
    kept = "पक्का Cheet जी, आज सुबह सवा नौ बजे का रख लेते हैं।"
    assert settles_a_time(kept, OFFERED) == "आज सुबह सवा नौ बजे"
    offer = "मेरे पास आज सुबह नौ बजे या सवा नौ बजे का समय खाली है। कौन सा ठीक रहेगा?"
    assert settles_a_time(offer, OFFERED) is None
    assert settles_a_time("आज सुबह सवा नौ बजे ठीक रहेगा?", OFFERED) is None


async def test_a_time_kept_without_booking_makes_the_model_book_it() -> None:
    built = voice_engine("पक्का जी, आज सुबह सवा नौ बजे का रख लेते हैं।", "जी।")
    session, state = call(clinic())
    state.language_state.primary_language = "hi"
    state.active_flow_state.offered_slots = OFFERED
    await turn(built, session, state, "सवा नौ बजे का")
    asked = built.model.requests[1].turns[-1]  # type: ignore[attr-defined]
    assert asked == UserTurn(BOOK_IT.format(time="आज सुबह सवा नौ बजे"))
