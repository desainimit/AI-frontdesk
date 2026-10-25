"""How an appointment number is shown, said and texted."""

from app.ai.speech_text import speakable
from app.ai.wording import spoken
from app.appointments.models import reference
from app.settings.defaults import default_settings
from app.settings.schemas import NotificationSettings


def test_the_number_is_shown_said_and_allowed_in_texts() -> None:
    assert reference(1042) == "A-1042"
    # Said letter by letter and digit by digit, in any language.
    assert speakable("Your appointment number is A-1042.", "en") == (
        "Your appointment number is A, 1 0 4 2."
    )
    booked = spoken("hi", "booked", time="कल शाम पाँच बजे", reference="A-7")
    assert "A-7" in booked
    # Clinics may put the number where they like in their own wording.
    settings = NotificationSettings.model_validate(
        default_settings("Lotus Clinic").notifications.model_dump()
        | {"confirmation_text": "Booked: {{appointment_number}} on {{starts_at}}."}
    )
    assert "{{appointment_number}}" in settings.confirmation_text
