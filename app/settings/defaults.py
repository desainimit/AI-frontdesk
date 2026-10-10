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
            greeting=f"Thank you for calling {clinic_name}. How can I help you today?",
            instructions=(
                "You are the front desk of a medical clinic. Help callers book, change or "
                "cancel appointments and answer questions about the clinic. Keep answers "
                "short and clear. Never give medical advice or discuss test results; offer "
                "to pass the request to the clinic team instead."
            ),
            unknown_answer=(
                "I do not have that information. I can pass your question to the clinic "
                "team so they can get back to you."
            ),
        ),
        calls=CallSettings(),
        scheduling=SchedulingSettings(),
        notifications=NotificationSettings(
            confirmation_text=(
                "Hi {{patient_name}}, your appointment at {{clinic_name}} is confirmed for "
                "{{starts_at}}."
            ),
            reminder_text=(
                "Reminder: you have an appointment at {{clinic_name}} on {{starts_at}}. "
                "Reply to this message if you need to change it."
            ),
        ),
        privacy=PrivacySettings(),
        tickets=TicketSettings(categories=list(DEFAULT_TICKET_CATEGORIES)),
    )
