"""Sounding like a person: spoken numbers, tone, a name, and asking again when unsure."""

import asyncio
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.ai.prompts import system_prompt
from app.ai.speech_text import speakable
from app.ai.wording import spoken
from app.conversation.manager import ConversationStateManager
from app.settings.defaults import default_settings
from app.settings.schemas import ReceptionistSettings
from app.voice.stt.base import TranscriptionResult
from app.voice.voices import list_voices
from tests.test_voice_latency import ScriptedEngine, receptionist_for, started_pipeline
from tests.voice_sim import Line, TimedTTS


@pytest.mark.parametrize(
    ("written", "said"),
    [
        ("See you at 10:30.", "See you at ten thirty."),
        ("Your slot is 9:00 AM.", "Your slot is nine a.m."),
        ("We close at 18:00.", "We close at six p.m."),
        ("At 4:05 pm", "At four oh five p.m."),
        ("Call +91 98765 43210", "Call plus 9 1, 9 8 7 6 5, 4 3 2 1 0"),
        ("Your code is 12345678.", "Your code is 1 2 3 4 5 6 7 8."),
        ("Dr. Shah & Dr. Rao", "Doctor Shah and Doctor Rao"),
        ("Room 12 on floor 3", "Room 12 on floor 3"),
        ("2026-10-25", "2026-10-25"),
    ],
)
def test_text_is_spoken_the_way_people_read_it(written: str, said: str) -> None:
    assert speakable(written, "en") == said


def test_other_languages_only_get_numbers_read_digit_by_digit() -> None:
    assert speakable("10:30 पर 98765 43210", "hi") == "10:30 पर 9 8 7 6 5, 4 3 2 1 0"


@pytest.mark.asyncio
async def test_the_caller_hears_the_spoken_form_and_the_transcript_keeps_the_written_one() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.0)
    pipeline = await started_pipeline(line, tts)
    try:
        speech = await pipeline.prepare("Monday at 10:30 with Dr. Shah.")
        assert await pipeline.play(speech)
    finally:
        await pipeline.close()
    assert tts.requests == ["Monday at ten thirty with Doctor Shah."]
    assert speech.text == "Monday at 10:30 with Dr. Shah."


def prompt_for(**receptionist: Any) -> str:
    configuration = default_settings("Lotus Clinic")
    configuration = configuration.model_copy(
        update={
            "receptionist": configuration.receptionist.model_copy(update=receptionist),
        }
    )
    session = MagicMock(configuration=configuration, channel="voice", contact=None)
    state = ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA" + "0" * 32)
    return system_prompt(session, state)


def test_prompt_carries_tone_and_name() -> None:
    concise = prompt_for(tone="concise", persona_name="Asha")
    assert "Tone: brief." in concise
    assert "Your name is Asha." in concise
    friendly = prompt_for()
    assert "Tone: warm" in friendly and "Your name is" not in friendly
    # Honest when asked, never claims to be human.
    plain = " ".join(friendly.split())
    assert "AI assistant" in plain and "Never pretend to be human" in plain


def test_voice_and_name_settings_are_checked() -> None:
    base = default_settings("Lotus Clinic").receptionist.model_dump()
    assert ReceptionistSettings.model_validate(base | {"voice_id": "a" * 20}).voice_id == "a" * 20
    for bad in ({"voice_id": "../v1/user"}, {"persona_name": "{{inject}}"}):
        with pytest.raises(ValidationError):
            ReceptionistSettings.model_validate(base | bad)


@pytest.mark.asyncio
async def test_unclear_speech_is_asked_again_once_then_answered() -> None:
    line, tts = Line(), TimedTTS(first_byte=0.0)
    engine = ScriptedEngine([])
    asked: list[str] = []
    original = engine.run

    def run(session: Any, state: Any, utterance: str) -> Any:
        asked.append(utterance)
        return original(session, state, utterance)

    engine.run = run  # type: ignore[method-assign]
    receptionist = receptionist_for(engine)
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("bok mundy", True, confidence=0.3))
        await receptionist.reply(TranscriptionResult("book monday", True, confidence=0.35))
        await asyncio.sleep(0.05)
    finally:
        await pipeline.close()
    assert tts.requests[0] == spoken("en", "line_unclear")
    # A second unclear answer goes to the model rather than asking a third time.
    assert asked == ["book monday"]


@pytest.mark.asyncio
async def test_voices_are_listed_with_their_samples() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == "el"
        return httpx.Response(
            200,
            json={
                "voices": [
                    {
                        "voice_id": "abcdefghij0123456789",
                        "name": "Asha",
                        "labels": {"accent": "indian", "gender": "female"},
                        "preview_url": "https://storage.example/asha.mp3",
                    },
                    {"name": "missing id"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
        voices = await list_voices(SecretStr("el"), client=client)
        assert [voice.id for voice in voices] == ["abcdefghij0123456789"]
        assert voices[0].description == "indian, female"
        refused = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401)))
        assert await list_voices(SecretStr("el"), client=refused) == []
        await refused.aclose()
