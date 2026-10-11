"""What a clinic can change about how it works, one section per settings page."""

import re
from datetime import date, datetime, time
from enum import StrEnum
from itertools import pairwise
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.ai.catalog import APPOINTMENT_TOOLS, SAFE_READ_TOOLS, SUPPORT_TOOLS
from app.calls.verification import VerificationPolicy
from app.conversation.locales import LanguagePolicy

TEMPLATE_VARIABLES = frozenset(
    {"patient_name", "starts_at", "clinic_name", "provider_name", "service_name"}
)


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClinicSettings(Section):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    email: str = Field(default="", max_length=254, pattern=r"^(|[^\s@]+@[^\s@]+\.[^\s@]+)$")
    phone: str = Field(default="", max_length=20, pattern=r"^(|\+[1-9][0-9]{7,14})$")
    address: str = Field(default="", max_length=1000)
    timezone: str = "Asia/Kolkata"
    website: str = Field(default="", max_length=500, pattern=r"^(https://[^\s]+)?$")

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Select an IANA time zone") from None
        return value


class OpeningWindow(Section):
    weekday: int = Field(ge=0, le=6)  # 0 is Monday
    opens: time
    closes: time

    @model_validator(mode="after")
    def valid_window(self) -> "OpeningWindow":
        if self.opens.tzinfo is not None or self.closes.tzinfo is not None:
            raise ValueError("Opening hours are local times without a UTC offset")
        if self.opens >= self.closes:
            raise ValueError("Closing time must be after opening time")
        return self


class HoursSettings(Section):
    windows: list[OpeningWindow] = Field(default_factory=list[OpeningWindow], max_length=42)
    holidays: list[date] = Field(default_factory=list[date], max_length=366)
    after_hours: Literal["answer_only", "callback", "transfer"] = "answer_only"

    @model_validator(mode="after")
    def nonoverlapping(self) -> "HoursSettings":
        for day in range(7):
            windows = sorted((w for w in self.windows if w.weekday == day), key=lambda w: w.opens)
            if any(a.closes > b.opens for a, b in pairwise(windows)):
                raise ValueError("Opening hours on the same day cannot overlap")
        if len(set(self.holidays)) != len(self.holidays):
            raise ValueError("Each holiday can be listed once")
        return self


class ReceptionistSettings(Section):
    greeting: str = Field(min_length=1, max_length=1000)
    tone: Literal["professional", "friendly", "concise"] = "friendly"
    instructions: str = Field(default="", max_length=4000)
    unknown_answer: str = Field(min_length=1, max_length=1000)
    # What the receptionist may do on its own; everything else is passed to staff.
    can_book: bool = True
    can_answer_from_knowledge: bool = True
    languages: LanguagePolicy = Field(
        default_factory=lambda: LanguagePolicy(default_language="en", enabled_languages=["en"])
    )

    @property
    def allowed_tools(self) -> frozenset[str]:
        tools = set(SAFE_READ_TOOLS | SUPPORT_TOOLS)
        if not self.can_answer_from_knowledge:
            tools.discard("get_knowledge")
        if self.can_book:
            tools |= APPOINTMENT_TOOLS
        return frozenset(tools)


class CallSettings(Section):
    enabled: bool = True
    max_duration_minutes: int = Field(default=20, ge=1, le=60)
    # Where a caller who asks for a person is sent; empty means no transfer.
    transfer_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")


class SchedulingSettings(Section):
    # Used when a booking names no appointment type.
    default_appointment_minutes: int = Field(default=30, ge=5, le=240, multiple_of=5)
    minimum_notice_minutes: int = Field(default=60, ge=0, le=43200)
    booking_horizon_days: int = Field(default=60, ge=1, le=365)
    cancellation_notice_minutes: int = Field(default=120, ge=0, le=43200)
    rescheduling_enabled: bool = True


class NotificationSettings(Section):
    recipients: list[str] = Field(default_factory=list[str], max_length=20)
    confirmations_enabled: bool = True
    reminders_enabled: bool = True
    reminder_hours_before: int = Field(default=24, ge=1, le=168)
    confirmation_text: str = Field(min_length=1, max_length=1000)
    reminder_text: str = Field(min_length=1, max_length=1000)
    cancellation_text: str = Field(
        default="Hi {{patient_name}}, your appointment at {{clinic_name}} on {{starts_at}} "
        "has been cancelled.",
        min_length=1,
        max_length=1000,
    )

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) for value in values):
            raise ValueError("Use valid email addresses")
        if len({value.lower() for value in values}) != len(values):
            raise ValueError("Each address can be listed once")
        return values

    @field_validator("confirmation_text", "reminder_text", "cancellation_text")
    @classmethod
    def valid_variables(cls, value: str) -> str:
        if not set(re.findall(r"\{\{\s*([^}]+?)\s*\}\}", value)) <= TEMPLATE_VARIABLES:
            raise ValueError("Use only: " + ", ".join(sorted(TEMPLATE_VARIABLES)))
        return value


class PrivacySettings(Section):
    # Call transcripts older than this are deleted by the worker. Calls are not recorded.
    transcript_retention_days: int = Field(default=90, ge=1, le=365)
    caller_verification: VerificationPolicy = Field(default_factory=VerificationPolicy)
    # On a call, the number shown can be faked. Off: a matching number is enough to
    # book, but seeing, moving or cancelling an appointment needs a code from reception
    # or goes to staff. By text message a matching number is always enough: the reply
    # only ever reaches that phone.
    known_number_can_manage: bool = False
    # Off: an event in a provider's own calendar says only what kind of visit it is.
    calendar_shows_patient_name: bool = False


class TicketSettings(Section):
    categories: list[str] = Field(min_length=1, max_length=30)

    @field_validator("categories")
    @classmethod
    def valid_categories(cls, values: list[str]) -> list[str]:
        if any(not 1 <= len(value) <= 60 for value in values):
            raise ValueError("A category name has 1 to 60 characters")
        if len({value.lower() for value in values}) != len(values):
            raise ValueError("Each category can be listed once")
        return values


class ClinicConfiguration(Section):
    """Everything in `tenant_settings.document`."""

    clinic: ClinicSettings
    hours: HoursSettings
    receptionist: ReceptionistSettings
    calls: CallSettings
    scheduling: SchedulingSettings
    notifications: NotificationSettings
    privacy: PrivacySettings
    tickets: TicketSettings

    def is_open(self, instant: datetime) -> bool:
        if instant.tzinfo is None:
            raise ValueError("Use an aware instant")
        local = instant.astimezone(ZoneInfo(self.clinic.timezone))
        if local.date() in self.hours.holidays:
            return False
        return any(
            window.weekday == local.weekday() and window.opens <= local.time() < window.closes
            for window in self.hours.windows
        )


class SettingsSection(StrEnum):
    CLINIC = "clinic"
    HOURS = "hours"
    RECEPTIONIST = "receptionist"
    CALLS = "calls"
    SCHEDULING = "scheduling"
    NOTIFICATIONS = "notifications"
    PRIVACY = "privacy"
    TICKETS = "tickets"


SECTION_MODELS: dict[SettingsSection, type[Section]] = {
    SettingsSection.CLINIC: ClinicSettings,
    SettingsSection.HOURS: HoursSettings,
    SettingsSection.RECEPTIONIST: ReceptionistSettings,
    SettingsSection.CALLS: CallSettings,
    SettingsSection.SCHEDULING: SchedulingSettings,
    SettingsSection.NOTIFICATIONS: NotificationSettings,
    SettingsSection.PRIVACY: PrivacySettings,
    SettingsSection.TICKETS: TicketSettings,
}


class SettingsView(Section):
    version: int
    updated_at: datetime
    settings: ClinicConfiguration


class FieldChange(Section):
    field: str
    before: JsonValue
    after: JsonValue


class SettingsChangeView(Section):
    id: UUID
    section: SettingsSection
    version: int
    changed_by: str
    created_at: datetime
    changes: list[FieldChange]


class SettingsChangePage(Section):
    items: list[SettingsChangeView]
    next_cursor: str | None
