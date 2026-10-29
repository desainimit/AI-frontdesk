"""What a clinic can change about how it works, one section per settings page."""

import re
from datetime import date, datetime, time
from enum import StrEnum
from itertools import pairwise
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.ai.catalog import APPOINTMENT_TOOLS, SAFE_READ_TOOLS, SUPPORT_TOOLS
from app.calls.verification import VerificationPolicy
from app.conversation.locales import LanguagePolicy
from app.languages import family, known, normalize, pack

TEMPLATE_VARIABLES = frozenset(
    {
        "patient_name",
        "starts_at",
        "clinic_name",
        "provider_name",
        "service_name",
        "appointment_number",
    }
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
    # The name the receptionist gives when asked; empty, it does not use one.
    persona_name: str = Field(default="", max_length=40, pattern=r"^[^\n{}<>]*$")
    # An ElevenLabs voice for this clinic; empty, the voice connection's own voice.
    voice_id: str = Field(default="", pattern=r"^([A-Za-z0-9]{20})?$")
    instructions: str = Field(default="", max_length=4000)
    unknown_answer: str = Field(min_length=1, max_length=1000)
    # What the receptionist may do on its own; everything else is passed to staff.
    can_book: bool = True
    can_answer_from_knowledge: bool = True
    languages: LanguagePolicy = Field(
        default_factory=lambda: LanguagePolicy(default_language="en", enabled_languages=["en"])
    )
    # The greeting and the "I don't know" line in the clinic's other languages, by
    # language code. `greeting` and `unknown_answer` are in its main language; a
    # language without its own here uses the product's wording for it.
    greetings: dict[str, str] = Field(default_factory=dict[str, str], max_length=8)
    unknown_answers: dict[str, str] = Field(default_factory=dict[str, str], max_length=8)

    @field_validator("greetings", "unknown_answers")
    @classmethod
    def by_language(cls, values: dict[str, str]) -> dict[str, str]:
        return _by_language(values)

    def greeting_in(self, language: str, clinic_name: str) -> str:
        return self._in(language, self.greeting, self.greetings, "greeting", clinic_name)

    def unknown_answer_in(self, language: str, clinic_name: str) -> str:
        return self._in(
            language, self.unknown_answer, self.unknown_answers, "unknown_answer", clinic_name
        )

    def _in(self, language: str, main: str, own: dict[str, str], key: str, clinic_name: str) -> str:
        code = normalize(language)
        if own.get(code):
            return own[code]
        if family(code) == family(self.languages.default_language) and code == normalize(
            self.languages.default_language
        ):
            return main
        return pack(code).clinic_texts[key].format(clinic=clinic_name)

    @property
    def allowed_tools(self) -> frozenset[str]:
        tools = set(SAFE_READ_TOOLS | SUPPORT_TOOLS)
        if not self.can_answer_from_knowledge:
            tools.discard("get_knowledge")
        if self.can_book:
            tools |= APPOINTMENT_TOOLS
        return frozenset(tools)


class TransferTarget(Section):
    """Somewhere a caller can be put through to: the front desk, billing, an on-call line."""

    name: str = Field(min_length=1, max_length=60, pattern=r"^[^\n{}<>]*$")
    number: str = Field(pattern=r"^\+[1-9][0-9]{7,14}$")
    # What callers should be put through here for; the receptionist chooses by this.
    description: str = Field(default="", max_length=200, pattern=r"^[^\n{}<>]*$")
    # "open": only while the clinic is open; "closed": only outside opening hours, as for
    # an on-call line; "always": either.
    when: Literal["always", "open", "closed"] = "always"


class CallSettings(Section):
    enabled: bool = True
    max_duration_minutes: int = Field(default=20, ge=1, le=60)
    # Where a caller who asks for a person can be put through, first one by default;
    # empty means no transfers. The receptionist always asks the caller first.
    transfer_targets: list[TransferTarget] = Field(
        default_factory=list[TransferTarget], max_length=10
    )

    @model_validator(mode="before")
    @classmethod
    def single_number(cls, value: object) -> object:
        # Settings saved before several targets existed had one number.
        if not isinstance(value, dict):
            return value
        data = cast(dict[str, Any], value).copy()
        if "transfer_number" in data:
            number = data.pop("transfer_number")
            if number and not data.get("transfer_targets"):
                data["transfer_targets"] = [{"name": "the front desk", "number": number}]
        return data

    @model_validator(mode="after")
    def distinct_names(self) -> "CallSettings":
        names = [target.name.casefold() for target in self.transfer_targets]
        if len(set(names)) != len(names):
            raise ValueError("Each transfer target needs its own name")
        return self


class SchedulingSettings(Section):
    # Used when a booking names no appointment type.
    default_appointment_minutes: int = Field(default=30, ge=5, le=240, multiple_of=5)
    minimum_notice_minutes: int = Field(default=60, ge=0, le=43200)
    booking_horizon_days: int = Field(default=60, ge=1, le=365)
    cancellation_notice_minutes: int = Field(default=120, ge=0, le=43200)
    rescheduling_enabled: bool = True


def _by_language(values: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for code, text in values.items():
        if not known(code):
            raise ValueError(f"Unknown language: {code}")
        if len(text) > 1000:
            raise ValueError("Keep each text under 1000 characters")
        if text.strip():
            cleaned[normalize(code)] = text.strip()
    return cleaned


MessageKind = Literal["confirmation_text", "reminder_text", "cancellation_text"]


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

    # Send texts through this Twilio Messaging Service (MG...) instead of from the
    # clinic's number: needed for an Indian DLT-registered sender ID or a short code.
    messaging_service_sid: str = Field(default="", pattern=r"^(MG[0-9a-fA-F]{32})?$")
    # The three texts in the clinic's other languages: language -> kind -> text. The
    # texts above are in its main language; a language without its own here uses the
    # product's wording for it.
    translations: dict[str, dict[MessageKind, str]] = Field(
        default_factory=dict[str, dict[MessageKind, str]], max_length=8
    )

    @field_validator("confirmation_text", "reminder_text", "cancellation_text")
    @classmethod
    def valid_variables(cls, value: str) -> str:
        if not set(re.findall(r"\{\{\s*([^}]+?)\s*\}\}", value)) <= TEMPLATE_VARIABLES:
            raise ValueError("Use only: " + ", ".join(sorted(TEMPLATE_VARIABLES)))
        return value

    @field_validator("translations")
    @classmethod
    def valid_translations(
        cls, values: dict[str, dict[MessageKind, str]]
    ) -> dict[str, dict[MessageKind, str]]:
        cleaned: dict[str, dict[MessageKind, str]] = {}
        for code, texts in values.items():
            if not known(code):
                raise ValueError(f"Unknown language: {code}")
            kept: dict[MessageKind, str] = {
                kind: cls.valid_variables(text.strip())
                for kind, text in texts.items()
                if text.strip()
            }
            if any(len(text) > 1000 for text in kept.values()):
                raise ValueError("Keep each text under 1000 characters")
            if kept:
                cleaned[normalize(code)] = kept
        return cleaned

    def text(self, kind: MessageKind, language: str, main_language: str) -> str:
        """The clinic's wording of a message in a language: its own, its main-language
        text when that is the language, or the product's wording for that language."""
        code = normalize(language)
        own = self.translations.get(code, {}).get(kind)
        if own:
            return own
        if code == normalize(main_language):
            return getattr(self, kind)
        return pack(code).clinic_texts[kind]


class PrivacySettings(Section):
    # Call transcripts older than this are deleted by the worker. Calls are not recorded.
    transcript_retention_days: int = Field(default=90, ge=1, le=365)
    caller_verification: VerificationPolicy = Field(default_factory=VerificationPolicy)
    # On a call, the number shown can be faked. Off: a matching number is enough to
    # book, but seeing, moving or cancelling an appointment needs a code from reception
    # or goes to staff. By text message a matching number is always enough: the reply
    # only ever reaches that phone.
    known_number_can_manage: bool = False
    # On: a caller can still see, move and cancel their own appointments by giving the
    # name on the appointment (and its number, when they call from another phone), the
    # way they would at the desk. Off: that needs a code from reception or goes to staff.
    confirm_callers_by_name: bool = True
    # On: once a caller is verified, the receptionist is reminded of their last calls and
    # coming appointments, so they are not asked everything again.
    remember_callers: bool = True
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

    def transfer_targets_now(self, instant: datetime) -> list[TransferTarget]:
        """Where a caller can be put through at this moment, the default first."""
        is_open = self.is_open(instant)
        if not is_open and self.hours.after_hours != "transfer":
            return []
        return [
            target
            for target in self.calls.transfer_targets
            if target.when == "always" or (target.when == "open") == is_open
        ]

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
