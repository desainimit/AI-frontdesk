"""Every language is complete, and the newly covered ones are understood."""

import re
from dataclasses import fields
from string import Formatter

import pytest

from app.ai.caller_check import appointment_number
from app.calls.access import normalize_access_code
from app.conversation.backchannel import wants_to_end
from app.conversation.confirmation import confirmation_answer
from app.conversation.language import asked_for_language
from app.conversation.state import SafetyDecision
from app.conversation.turn_end import sounds_unfinished
from app.languages import CODES, LANGUAGES, family, normalize, pack
from app.languages.types import LanguagePack, SafetyWords
from app.messaging.service import START_WORDS, STOP_WORDS
from app.safety.messages import emergency_message
from app.safety.numbers import clinic_country, emergency_lines
from app.safety.rules import deterministic_rule

# Hindi and Gujarati end a polite request with "na"; no other language has it.
MAY_BE_EMPTY = {"soft_no", "transfer_agree", "latin_words", "clock"}


def placeholders(value: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(value) if name}


@pytest.mark.parametrize("code", CODES)
def test_every_pack_has_every_line_with_the_same_placeholders(code: str) -> None:
    english, words = pack("en"), pack(code)
    for name in ("messages", "handover", "safety"):
        theirs, ours = getattr(words, name), getattr(english, name)
        assert theirs.keys() == ours.keys(), name
        for key, template in theirs.items():
            assert template.strip(), (name, key)
            assert placeholders(template) == placeholders(ours[key]), (name, key)
    assert words.narration.keys() == english.narration.keys()
    assert all(words.narration[tool] for tool in english.narration)
    assert placeholders(words.emergency_with_number) == {"number"}
    assert placeholders(words.crisis) == {"crisis", "number"}


@pytest.mark.parametrize("code", CODES)
def test_every_list_is_filled_and_every_pattern_compiles(code: str) -> None:
    words: LanguagePack = pack(code)
    for item in fields(words):
        if item.name not in MAY_BE_EMPTY:
            assert getattr(words, item.name), item.name
    for item in fields(SafetyWords):
        part = getattr(words.safety_words, item.name)
        assert part, item.name
        re.compile(part)
    for part in (words.openers, words.serious, words.ending, words.anything_else):
        re.compile(part)
    assert len(words.weekdays) == 7 and len(words.months) == 12
    assert len(words.digit_words) == 10
    # Hindi, Hinglish and Gujarati say the time the Indian way.
    assert (words.clock is not None) == (family(code) in {"hi", "gu"})


def test_codes_are_bcp47_and_old_names_still_mean_the_same_language() -> None:
    assert normalize("hinglish") == normalize("hi-latn") == "hi-Latn"
    assert normalize("gu-IN") == "gu" and normalize("en_GB") == "en"
    assert family("hi-Latn") == "hi"
    assert LANGUAGES["en"].locale("IN") == "en-IN" and LANGUAGES["en"].locale(None) == "en-US"


@pytest.mark.parametrize(
    ("said", "reason"),
    [
        ("No puedo respirar", "breathing"),
        ("Quiero morir", "self_harm"),
        ("J'ai une douleur thoracique", "chest"),
        ("Je veux mourir", "self_harm"),
        ("Ich will sterben", "self_harm"),
        ("Er ist bewusstlos", "neurologic"),
        ("Não consigo respirar", "breathing"),
        ("Tomei muitos comprimidos", "overdose"),
    ],
)
def test_emergencies_are_recognised_in_every_language(said: str, reason: str) -> None:
    result = deterministic_rule(said)
    assert (result.decision, result.reason) == (SafetyDecision.EMERGENCY_PROTOCOL, reason)


def test_a_denied_symptom_is_not_an_emergency() -> None:
    assert deterministic_rule("No tengo dolor de pecho").decision != (
        SafetyDecision.EMERGENCY_PROTOCOL
    )


def test_a_spanish_caller_in_crisis_hears_spains_line_in_spanish() -> None:
    lines = emergency_lines("+34911234567", "Europe/Madrid")
    said = emergency_message("es", lines, "self_harm")
    assert "0 2 4" in said and "1 1 2" in said and "Siento" in said


def test_most_of_the_americas_are_not_the_united_states() -> None:
    assert clinic_country("", "America/Sao_Paulo") == "BR"
    assert clinic_country("", "America/New_York") == "US"
    assert clinic_country("+5511999990000", "UTC") == "BR"


def test_yes_no_goodbye_and_pauses_in_the_newly_covered_languages() -> None:
    assert confirmation_answer("sí, claro") is True
    assert confirmation_answer("non, annulez") is False
    assert wants_to_end("adiós, gracias")
    assert wants_to_end("nein danke", "Kann ich noch etwas für Sie tun?")
    assert sounds_unfinished("Quería una cita para mi madre y", "es")
    # Another language's words do not hold an English caller up.
    assert not sounds_unfinished("I don't want to die", "en")


def test_digits_and_numbers_in_any_script() -> None:
    assert normalize_access_code("१२३४५६७८") == "12345678"
    assert normalize_access_code("૧૨૩૪૫૬૭૮") == "12345678"
    assert appointment_number("A teen", "hi-Latn") == 3
    assert appointment_number("A तीन", "hi") == 3
    # "um" is a pause in English, even beside a Portuguese digit word.
    assert appointment_number("um, A three", "en") == 3


def test_asking_for_a_language_in_another_one() -> None:
    assert asked_for_language("¿Puede hablar en inglés?", ("en", "es")) == "en"
    assert asked_for_language("Hinglish please", ("en", "hi")) == "hi-Latn"


def test_opting_out_works_in_each_language_and_in_english() -> None:
    assert {"STOP", "BAJA", "STOPP", "PARAR"} <= STOP_WORDS
    assert {"START", "ALTA"} <= START_WORDS
    # Cancelling an appointment by text never stops messages.
    assert "CANCELAR" not in STOP_WORDS
