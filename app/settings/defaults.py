"""What a new clinic starts with, so it works before anyone opens a settings page."""

from datetime import time

from app.settings.schemas import (
    CallSettings,
    ClinicConfiguration,
    ClinicSettings,
    HoursSettings,
    NotificationSettings,
    OpeningWindow,
    PrivacySettings,
    ReceptionistSettings,
    SchedulingSettings,
    TicketSettings,
)

DEFAULT_LOCATION_NAME = "Main clinic"
DEFAULT_TIMEZONE = "Asia/Kolkata"
# (name, minutes): the appointment types every new clinic gets.
DEFAULT_SERVICES = (("New patient visit", 30), ("Follow-up visit", 15))
DEFAULT_TICKET_CATEGORIES = [
    "Appointment request",
    "Prescription or refill",
    "Billing question",
    "Test results",
    "Callback request",
    "Other",
]


def default_hours() -> HoursSettings:
    """Monday to Friday 09:00-18:00 and Saturday 09:00-13:00."""
    weekdays = [OpeningWindow(weekday=day, opens=time(9), closes=time(18)) for day in range(5)]
    return HoursSettings(
        windows=[*weekdays, OpeningWindow(weekday=5, opens=time(9), closes=time(13))]
    )


def default_settings(clinic_name: str, timezone: str = DEFAULT_TIMEZONE) -> ClinicConfiguration:
    return ClinicConfiguration(
        clinic=ClinicSettings(name=clinic_name, timezone=timezone),
        hours=default_hours(),
        receptionist=ReceptionistSettings(
            # No "Hi": a caller the clinic knows has just heard "Hi Nimit!".
            greeting=f"Thanks for calling {clinic_name}! How can I help you today?",
            instructions=(
                "You are the front desk of this clinic, on the phone and by text. Be friendly, "
                "short and clear, like an experienced receptionist.\n\n"
                "Handle these yourself:\n"
                "- Booking, moving and cancelling appointments, and telling patients about "
                "their upcoming or past visits.\n"
                "- Questions about the clinic: hours, address and directions, parking, doctors, "
                "services, prices, insurance and payment, and how to prepare for a visit. Answer "
                "only from the clinic's details and knowledge base. If the answer is not there, "
                "say so and offer to have the team get back to them.\n\n"
                "Booking:\n"
                "- Ask what the visit is for in a few words, then which doctor, day or time "
                "suits them.\n"
                "- Offer two free times at most, the nearest first. If neither suits, offer the "
                "next ones, another doctor, or a callback when something opens up.\n"
                "- Get their name before you book and read the details back before confirming.\n"
                "- After booking, give their appointment number and anything the clinic asks "
                "patients to bring or do.\n\n"
                "Pass to the team (take a message, or offer to put a caller through):\n"
                "- Symptoms and medical questions, test results, prescriptions and refills, bills, "
                "refunds and complaints.\n"
                "- Anything you cannot find in the clinic's details.\n\n"
                "Always: never give medical advice, never guess, and never share another "
                "patient's details. If someone describes an emergency, tell them to call "
                "emergency services right away. Ask once at the end whether there is anything "
                "else, then close warmly."
            ),
            unknown_answer=(
                "Hmm, I don't have that in front of me. I can pass your question to the "
                "team so they get back to you."
            ),
        ),
        calls=CallSettings(),
        scheduling=SchedulingSettings(),
        notifications=NotificationSettings(
            confirmation_text=(
                "Hi {{patient_name}}, you're booked at {{clinic_name}} with {{provider_name}} "
                "on {{starts_at}}. Your appointment number is {{appointment_number}}. Reply to "
                "this message if you need to change it."
            ),
            reminder_text=(
                "Hi {{patient_name}}, a reminder of your appointment at {{clinic_name}} with "
                "{{provider_name}} on {{starts_at}} (appointment {{appointment_number}}). Reply "
                "to this message if you need to change it."
            ),
        ),
        privacy=PrivacySettings(),
        tickets=TicketSettings(categories=list(DEFAULT_TICKET_CATEGORIES)),
    )
