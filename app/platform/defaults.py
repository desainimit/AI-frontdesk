"""What every new clinic starts with, editable by a platform administrator.

Until an administrator saves their own version, the product's built-in defaults apply
(`app.settings.defaults`). Changing the defaults affects clinics created afterwards;
existing clinics keep their own settings.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.identity.sessions import platform_scope
from app.platform.models import PlatformAuditEvent, PlatformSettings
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.settings.defaults import DEFAULT_SERVICES, default_settings
from app.settings.schemas import (
    CallSettings,
    ClinicConfiguration,
    ClinicSettings,
    HoursSettings,
    NotificationSettings,
    PrivacySettings,
    ReceptionistSettings,
    SchedulingSettings,
    TicketSettings,
)

# Written where the clinic's own name belongs; filled in when a clinic is created.
CLINIC_NAME = "{{clinic_name}}"


class DefaultService(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)
    minutes: int = Field(ge=5, le=240, multiple_of=5)


class WorkspaceDefaults(BaseModel):
    """A new clinic's settings, minus the details that are its own (name, address)."""

    model_config = ConfigDict(extra="forbid")
    hours: HoursSettings
    receptionist: ReceptionistSettings
    calls: CallSettings
    scheduling: SchedulingSettings
    notifications: NotificationSettings
    privacy: PrivacySettings
    tickets: TicketSettings
    services: list[DefaultService] = Field(min_length=1, max_length=20)

    @field_validator("services")
    @classmethod
    def unique_names(cls, values: list[DefaultService]) -> list[DefaultService]:
        if len({item.name.lower() for item in values}) != len(values):
            raise ValueError("Each appointment type needs its own name")
        return values

    def settings_for(self, clinic_name: str, timezone: str) -> ClinicConfiguration:
        receptionist = self.receptionist.model_copy(
            update={
                field: getattr(self.receptionist, field).replace(CLINIC_NAME, clinic_name)
                for field in ("greeting", "instructions", "unknown_answer")
            }
        )
        return ClinicConfiguration(
            clinic=ClinicSettings(name=clinic_name, timezone=timezone),
            hours=self.hours,
            receptionist=receptionist,
            calls=self.calls,
            scheduling=self.scheduling,
            notifications=self.notifications,
            privacy=self.privacy,
            tickets=self.tickets,
        )


def product_defaults() -> WorkspaceDefaults:
    """The defaults the product ships with."""
    document = default_settings(CLINIC_NAME)
    return WorkspaceDefaults(
        **document.model_dump(exclude={"clinic"}),
        services=[DefaultService(name=name, minutes=minutes) for name, minutes in DEFAULT_SERVICES],
    )


async def load_defaults(session: AsyncSession) -> WorkspaceDefaults:
    """The administrators' saved defaults, or the product's. Needs the platform scope."""
    row = await session.get(PlatformSettings, 1)
    if row is None or row.workspace_defaults is None:
        return product_defaults()
    return WorkspaceDefaults.model_validate(row.workspace_defaults)


class DefaultsView(BaseModel):
    defaults: WorkspaceDefaults
    # False while the product's own defaults are in use.
    customised: bool


class WorkspaceDefaultsService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, context: PlatformContext) -> DefaultsView:
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await session.get(PlatformSettings, 1)
            return DefaultsView(
                defaults=await load_defaults(session),
                customised=row is not None and row.workspace_defaults is not None,
            )

    async def save(
        self, context: PlatformContext, command: WorkspaceDefaults | None
    ) -> DefaultsView:
        """Save new defaults, or with None go back to the product's."""
        require_platform_permission(context, PlatformPermission.TENANT_UPDATE)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            row = await session.get(PlatformSettings, 1, with_for_update=True)
            if row is None:
                raise RuntimeError("platform_settings has no row; run the setup job")
            row.workspace_defaults = command.model_dump(mode="json") if command else None
            session.add(
                PlatformAuditEvent(
                    user_id=context.user_id,
                    request_id=context.request_id,
                    action="workspace_defaults_saved" if command else "workspace_defaults_reset",
                )
            )
            await session.flush()
            return DefaultsView(
                defaults=command or product_defaults(), customised=command is not None
            )
