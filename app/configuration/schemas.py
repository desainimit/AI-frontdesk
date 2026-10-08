from datetime import date, datetime, time
from itertools import pairwise
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from app.agents.catalog import ALL_TOOLS, SAFE_READ_TOOLS
from app.auth.policies import Permission
from app.business.schemas import DirectorySnapshot
from app.configuration.sections import ConfigurationSection
from app.conversation.locales import LanguagePolicy


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BusinessSettings(Section):
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=2000)
    email: str = Field(default="", max_length=254, pattern=r"^(|[^\s@]+@[^\s@]+\.[^\s@]+)$")
    phone: str = Field(default="", max_length=20, pattern=r"^(|\+[1-9][0-9]{7,14})$")
    address: str = Field(default="", max_length=1000)
    timezone: str = "Asia/Kolkata"
    website: str = Field(default="", max_length=500, pattern=r"^(https://[^\s]+)?$")
    logo_object_key: str | None = Field(default=None, max_length=500)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Select an IANA time zone") from None
        return value


class OpeningWindow(Section):
    weekday: int = Field(ge=0, le=6)
    opens: time
    closes: time

    @model_validator(mode="after")
    def valid_window(self) -> "OpeningWindow":
        if self.opens.tzinfo is not None or self.closes.tzinfo is not None:
            raise ValueError("Opening hours use local wall-clock times without UTC offsets")
        if self.opens >= self.closes:
            raise ValueError("Split overnight opening hours into separate day windows")
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
                raise ValueError("Opening windows cannot overlap")
        if len(set(self.holidays)) != len(self.holidays):
            raise ValueError("Holidays must be unique")
        return self


class AgentSettings(Section):
    greeting: str = Field(default="Hello, how can I help you today?", min_length=1, max_length=1000)
    tone: Literal["professional", "friendly", "concise"] = "professional"
    instructions: str = Field(default="", max_length=4000)
    unknown_answer: str = Field(
        default="I do not have verified information about that. I can help you contact the team.",
        min_length=1,
        max_length=1000,
    )
    allowed_tools: list[str] = Field(default_factory=lambda: sorted(SAFE_READ_TOOLS), max_length=30)
    max_steps: int = Field(default=3, ge=1, le=6)
    max_tool_calls: int = Field(default=4, ge=1, le=10)

    @field_validator("allowed_tools")
    @classmethod
    def known_tools(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not set(value) <= ALL_TOOLS:
            raise ValueError("Select unique registered tools")
        return value


class VoiceSettings(Section):
    enabled: bool = False
    max_duration_minutes: int = Field(default=20, ge=1, le=60)
    transfer_number: str | None = Field(default=None, pattern=r"^\+[1-9][0-9]{7,14}$")
    no_answer: Literal["callback", "ticket", "message"] = "callback"
    primary_connection_id: UUID | None = None
    fallback_connection_id: UUID | None = None
    voice_id: str | None = Field(default=None, max_length=100)


class SchedulingSettings(Section):
    enabled: bool = False
    minimum_notice_minutes: int = Field(default=60, ge=0, le=43200)
    booking_horizon_days: int = Field(default=60, ge=1, le=365)
    cancellation_notice_minutes: int = Field(default=60, ge=0, le=43200)
    rescheduling_enabled: bool = True
    confirmations_enabled: bool = True
    calendar_connection_ids: list[UUID] = Field(default_factory=list[UUID], max_length=50)


class ChatSettings(Section):
    enabled: bool = False
    allowed_origins: list[str] = Field(default_factory=list, max_length=20)
    title: str = Field(default="Chat with us", min_length=1, max_length=100)
    greeting: str = Field(default="How can we help?", min_length=1, max_length=1000)
    accent_color: str = Field(default="#18181B", pattern=r"^#[0-9a-fA-F]{6}$")

    @field_validator("allowed_origins")
    @classmethod
    def valid_origins(cls, values: list[str]) -> list[str]:
        from urllib.parse import urlsplit

        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Widget domains must be HTTPS origins without a path")
        if len(values) != len(set(values)):
            raise ValueError("Widget domains must be unique")
        return values


class MessagingSettings(Section):
    sms_enabled: bool = False
    email_enabled: bool = False
    email_signature: str = Field(default="", max_length=2000)
    sender_name: str = Field(default="", max_length=100)
    connection_ids: list[UUID] = Field(default_factory=list[UUID], max_length=20)


class WorkflowSettings(Section):
    appointment_reminders: bool = False
    missed_call_follow_up: bool = False
    consented_callbacks: bool = False
    lead_assignment: bool = False
    ticket_escalation: bool = False
    post_call_sync: bool = False
    reminder_minutes_before: int = Field(default=60, ge=1, le=10080)
    follow_up_delay_minutes: int = Field(default=15, ge=1, le=10080)
    escalation_after_minutes: int = Field(default=60, ge=1, le=10080)
    quiet_hours_start: time = time(20)
    quiet_hours_end: time = time(8)
    maximum_attempts_per_day: int = Field(default=3, ge=1, le=3)
    default_assignee_id: UUID | None = None


class PrivacySettings(Section):
    recording_enabled: bool = False
    recording_disclosure: str = Field(default="", max_length=1000)
    transcript_retention_days: int = Field(default=90, ge=1, le=365)
    recording_retention_days: int = Field(default=30, ge=1, le=90)

    @model_validator(mode="after")
    def require_disclosure(self) -> "PrivacySettings":
        if self.recording_enabled and not self.recording_disclosure:
            raise ValueError("Recording requires a disclosure and runtime consent")
        return self


class BudgetSettings(Section):
    monthly_budget_paisa: int = Field(default=0, ge=0, le=1000000000)
    alert_percentages: list[int] = Field(default_factory=lambda: [50, 80, 100], max_length=5)
    auto_recharge: bool = False
    recharge_amount_paisa: int = Field(default=0, ge=0, le=10000000)

    @model_validator(mode="after")
    def valid_budget(self) -> "BudgetSettings":
        if len(set(self.alert_percentages)) != len(self.alert_percentages) or any(
            not 1 <= value <= 100 for value in self.alert_percentages
        ):
            raise ValueError("Alert thresholds must be unique percentages")
        if self.auto_recharge and self.recharge_amount_paisa <= 0:
            raise ValueError("Auto recharge requires an explicit positive amount")
        return self


class NotificationSettings(Section):
    recipients: list[str] = Field(default_factory=list, max_length=20)
    booking_template: str = Field(
        default="Your appointment is confirmed for {{starts_at}}.", max_length=2000
    )
    reminder_template: str = Field(
        default="Reminder: your appointment is at {{starts_at}}.", max_length=2000
    )

    @field_validator("recipients")
    @classmethod
    def valid_recipients(cls, values: list[str]) -> list[str]:
        if any(len(value) > 254 or "@" not in value or "\n" in value for value in values):
            raise ValueError("Use valid recipient email addresses")
        return values

    @field_validator("booking_template", "reminder_template")
    @classmethod
    def valid_variables(cls, value: str) -> str:
        import re

        if not set(re.findall(r"\{\{\s*([^}]+?)\s*\}\}", value)) <= {
            "starts_at",
            "ends_at",
            "business_name",
            "service_name",
            "location_name",
        }:
            raise ValueError("Use supported template variables only")
        return value


class LocationOverride(Section):
    timezone: str | None = None
    address: str | None = Field(default=None, max_length=1000)
    hours: HoursSettings | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        return BusinessSettings.valid_timezone(value) if value is not None else None


class AgentOverride(Section):
    location_id: UUID | None = None
    greeting: str | None = Field(default=None, min_length=1, max_length=1000)
    tone: Literal["professional", "friendly", "concise"] | None = None
    instructions: str | None = Field(default=None, max_length=4000)
    unknown_answer: str | None = Field(default=None, min_length=1, max_length=1000)
    allowed_tools: list[str] | None = Field(default=None, max_length=30)
    max_steps: int | None = Field(default=None, ge=1, le=6)
    max_tool_calls: int | None = Field(default=None, ge=1, le=10)
    languages: LanguagePolicy | None = None

    @field_validator("allowed_tools")
    @classmethod
    def known_tools(cls, value: list[str] | None) -> list[str] | None:
        return AgentSettings.known_tools(value) if value is not None else None


class ChannelOverride(Section):
    kind: Literal["voice", "chat"]
    location_id: UUID | None = None
    agent_id: UUID | None = None
    phone_number_id: UUID | None = None
    greeting: str | None = Field(default=None, min_length=1, max_length=1000)
    max_duration_minutes: int | None = Field(default=None, ge=1, le=60)
    # None inherits; an empty string explicitly removes a transfer destination.
    transfer_number: str | None = Field(default=None, pattern=r"^(|\+[1-9][0-9]{7,14})$")
    chat: ChatSettings | None = None

    @model_validator(mode="after")
    def compatible_fields(self) -> "ChannelOverride":
        if self.kind == "voice" and self.chat is not None:
            raise ValueError("Chat settings belong to chat channels")
        if self.kind == "chat" and any(
            value is not None
            for value in (self.phone_number_id, self.max_duration_minutes, self.transfer_number)
        ):
            raise ValueError("Phone settings belong to voice channels")
        return self


class ConfigurationScopes(Section):
    locations: dict[UUID, LocationOverride] = Field(
        default_factory=dict[UUID, LocationOverride], max_length=200
    )
    agents: dict[UUID, AgentOverride] = Field(
        default_factory=dict[UUID, AgentOverride], max_length=200
    )
    channels: dict[UUID, ChannelOverride] = Field(
        default_factory=dict[UUID, ChannelOverride], max_length=200
    )

    @model_validator(mode="after")
    def unique_phone_routes(self) -> "ConfigurationScopes":
        phones = [
            scope.phone_number_id for scope in self.channels.values() if scope.phone_number_id
        ]
        if len(set(phones)) != len(phones):
            raise ValueError("A phone number can route to only one channel")
        return self


class OperationalConfiguration(Section):
    schema_version: Literal[1] = 1
    business: BusinessSettings = Field(default_factory=BusinessSettings)
    hours: HoursSettings = Field(default_factory=HoursSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    languages: LanguagePolicy = Field(
        default_factory=lambda: LanguagePolicy(default_language="en", enabled_languages=["en"])
    )
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    scheduling: SchedulingSettings = Field(default_factory=SchedulingSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    messaging: MessagingSettings = Field(default_factory=MessagingSettings)
    workflows: WorkflowSettings = Field(default_factory=WorkflowSettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    scopes: ConfigurationScopes = Field(default_factory=ConfigurationScopes)
    directory: DirectorySnapshot = Field(default_factory=DirectorySnapshot)


class SaveConfiguration(Section):
    revision: int = Field(ge=1, strict=True)
    document: OperationalConfiguration


class ConfigurationRevision(Section):
    revision: int = Field(ge=1, strict=True)


class SaveConfigurationSection(ConfigurationRevision):
    value: JsonValue


MAX_SCOPE_WRITE_BATCH = 25


class ScopeChange(Section):
    kind: Literal["location", "agent", "channel"]
    id: UUID
    value: JsonValue


class SaveConfigurationScopes(ConfigurationRevision):
    changes: list[ScopeChange] = Field(min_length=1, max_length=MAX_SCOPE_WRITE_BATCH)

    @model_validator(mode="after")
    def unique_scopes(self) -> "SaveConfigurationScopes":
        if len({(change.kind, change.id) for change in self.changes}) != len(self.changes):
            raise ValueError("Change each scope once per request")
        return self


class ConfigurationScopesResult(ConfigurationRevision):
    changes: list[ScopeChange]


class ConfigurationSectionView(Section):
    section: ConfigurationSection
    revision: int
    published_version: int | None
    published_revision: int | None
    value: JsonValue
    published_value: JsonValue
    read_permission: Permission
    update_permission: Permission
    publish_permission: Permission


class ConfigurationScopeView(ConfigurationSectionView):
    kind: Literal["location", "agent", "channel"]
    id: UUID
    name: str
    is_active: bool
    inherited_values: dict[str, JsonValue]
    effective_values: dict[str, JsonValue]
    field_origins: dict[str, str]


class PublicationSummary(Section):
    version: int
    source_revision: int
    content_hash: str


class ConfigurationHead(Section):
    tenant_id: UUID
    draft_revision: int
    published_version: int | None


class ConfigurationView(Section):
    revision: int
    defaults_version: int
    published_version: int | None
    document: OperationalConfiguration


class ValidationIssue(Section):
    field: str
    code: str
    message: str


class ConfigurationValidation(Section):
    revision: int
    valid: bool
    issues: list[ValidationIssue]


class ConfigurationChange(Section):
    field: str
    label: str | None = None
    kind: Literal["added", "changed", "removed"]
    published: JsonValue
    draft: JsonValue


class ConfigurationDiff(Section):
    revision: int
    published_version: int | None
    changes: list[ConfigurationChange]
    validation: ConfigurationValidation
    impact: list[str]


class ConfigurationVersionView(Section):
    version: int
    source_revision: int
    content_hash: str
    document: OperationalConfiguration


class ConfigurationTest(Section):
    revision: int = Field(ge=1, strict=True)
    instant: datetime

    @field_validator("instant")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Use an instant with an explicit UTC offset")
        return value


class ConfigurationTestView(Section):
    revision: int
    validation: ConfigurationValidation
    instant: datetime
    business_open: bool
    timezone: str
    greeting: str
    language: str
    tools: list[str]
    duration_minutes: int
    external_actions_executed: Literal[False] = False


class EffectiveConfigurationView(Section):
    version: int
    document: OperationalConfiguration
    origin: Literal["tenant"] = "tenant"
    field_origins: dict[str, str] = Field(default_factory=dict)
    location_id: UUID | None = None
    agent_id: UUID | None = None
    channel_id: UUID | None = None
