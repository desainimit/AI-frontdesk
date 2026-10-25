"""A noisy line, and a caller asking about their own appointments."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.ai.tools.appointments import LISTED_APPOINTMENTS, AppointmentsInput
from app.ai.tools.toolbox import build_tools
from app.conversation.state import IdentifierType
from app.voice.processors.interruption import InterruptionHandler
from app.voice.stt.base import TranscriptionResult
from tests.test_voice_interruptions import Engine, call, until
from tests.test_voice_transfer import call as voice_call
from tests.test_voice_transfer import clinic


def frame(amplitude: int) -> bytes:
    """20 ms of PCM16 at a steady loudness."""
    return amplitude.to_bytes(2, "little", signed=True) * 160


def test_a_steady_noisy_line_stops_counting_as_speech() -> None:
    handler = InterruptionHandler("MZ" + "0" * 32)
    traffic = frame(600)
    assert handler.hear(traffic)  # at first it is taken for speech...
    for _ in range(1500):  # ...about 30 seconds of the same noise later,
        handler.hear(traffic)
    assert not handler.hear(traffic)  # it is background,
    assert handler.hear(frame(3000))  # while a caller speaking up still counts.
    # Once the line goes quiet, quieter speech counts again soon.
    for _ in range(100):
        handler.hear(frame(0))
    assert handler.hear(frame(800))


def test_a_noisy_line_does_not_cut_the_receptionist_off() -> None:
    handler = InterruptionHandler("MZ" + "0" * 32)
    for _ in range(1500):
        handler.hear(frame(600))
    handler.set_synthesizing(True)
    for _ in range(60):
        handler.process_incoming_audio(frame(600))
    assert not handler.is_interrupted


async def test_half_heard_words_while_it_talks_are_taken_for_background() -> None:
    pipeline, receptionist, stt, line = await call(
        Engine(("Sure, I can help you with that today.", "Dr. Shah has time on Monday."))
    )
    try:
        stt.stream.say("When can I come in?")
        await asyncio.sleep(0.5)
        # The TV in the background, half-heard by the recognizer.
        stt.stream._queue.put_nowait(  # pyright: ignore[reportPrivateUsage]
            TranscriptionResult("and the weather tomorrow", False, confidence=0.3)
        )
        await until(lambda: any(t.speaker == "agent" for t in receptionist.state.transcript_turns))
    finally:
        await pipeline.close()
    assert not line.cleared()


class Appointments:
    """Stands in for the appointment service and notes what was asked."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    async def list(self, context: Any, limit: int, offset: int, **filters: Any) -> list[Any]:
        self.asked.append({"limit": limit, **filters})
        row = MagicMock(
            id=uuid4(),
            provider_id=uuid4(),
            provider_name="Dr. Rao",
            service_name="Follow-up visit",
            starts_at=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
            status="booked",
        )
        return [row]


@pytest.mark.parametrize("which", ["upcoming", "past"])
async def test_a_caller_hears_their_appointments_the_spoken_way(which: str) -> None:
    _, flow, _ = build_tools(MagicMock(), MagicMock(), None)
    appointments = Appointments()
    flow.appointments = appointments  # type: ignore[assignment]
    session, state = voice_call(clinic())
    state.identity_state.record_attempt(True, uuid4())
    state.identity_state.identifier_type = IdentifierType.ACCESS_CODE
    result = await flow.list_appointments(session, state, AppointmentsInput(which=which))
    item = result.model_dump()["items"][0]
    assert item["doctor"] == "Dr. Rao" and item["service"] == "Follow-up visit"
    assert "Oct" not in item["when"] and "p.m." in item["when"]  # spoken, not "Tue 27 Oct"
    asked = appointments.asked[0]
    assert asked["limit"] == LISTED_APPOINTMENTS
    if which == "past":
        assert asked["newest_first"] is True and asked["starts_to"] <= datetime.now(UTC)
    else:
        assert asked["status"] == "booked"
        assert asked["starts_from"] >= datetime.now(UTC) - timedelta(seconds=5)
