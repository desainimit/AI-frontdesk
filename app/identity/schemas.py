from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

EMAIL_PATTERN = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
# What the browser should show next after a sign-in step.
NextStep = Literal["mfa", "choose_workspace", "workspace", "platform"]


class _Command(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, str_strip_whitespace=True)


def _normalized_email(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _checked_password(value: SecretStr) -> SecretStr:
    text = value.get_secret_value().casefold()
    if len(set(text)) < 5 or text in {"password1234", "123456789012", "qwertyuiopas"}:
        raise ValueError("Choose a less predictable password")
    return value


class Login(_Command):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    password: SecretStr = Field(min_length=1, max_length=256)

    _email = field_validator("email", mode="before")(_normalized_email)


class MfaProof(_Command):
    """A six-digit authenticator code, or one recovery code when the phone is unavailable."""

    code: SecretStr | None = Field(default=None, min_length=6, max_length=6)
    recovery_code: SecretStr | None = Field(default=None, min_length=32, max_length=64)

    @model_validator(mode="after")
    def exactly_one(self) -> "MfaProof":
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("Enter an authenticator code or a recovery code")
        return self


class MfaCode(_Command):
    code: SecretStr = Field(min_length=6, max_length=6)


class DisableMfa(MfaProof):
    password: SecretStr = Field(min_length=1, max_length=256)


class NewPassword(_Command):
    password: SecretStr = Field(min_length=12, max_length=128)

    _password = field_validator("password")(_checked_password)


class ChangePassword(NewPassword):
    current_password: SecretStr = Field(min_length=1, max_length=256)


class ForgotPassword(_Command):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)

    _email = field_validator("email", mode="before")(_normalized_email)


class LinkToken(_Command):
    token: SecretStr = Field(min_length=1, max_length=100)


class ResetPassword(LinkToken, NewPassword):
    pass


class AcceptInvitation(LinkToken):
    # Required for a new account; an existing account keeps its password.
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)

    @field_validator("password")
    @classmethod
    def password_policy(cls, value: SecretStr | None) -> SecretStr | None:
        return _checked_password(value) if value is not None else None


class SelectWorkspace(_Command):
    workspace_id: UUID


class OpenWorkspace(_Command):
    # Shown in the activity log; the one thing always required.
    reason: str = Field(min_length=3, max_length=500)


class InviteMember(_Command):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="member", pattern="^(admin|member)$")
    role_id: UUID | None = None

    _email = field_validator("email", mode="before")(_normalized_email)


class TeamMemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: str = Field(pattern="^(admin|member)$")
    is_active: bool
    role_id: UUID | None = None


class SessionView(BaseModel):
    # The token appears only in this response, never in logs or audit records.
    access_token: str = Field(repr=False)
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    next: NextStep


class WorkspaceOption(BaseModel):
    id: UUID
    name: str
    service_state: str


class SupportVisit(BaseModel):
    id: UUID
    reason: str
    expires_at: datetime


class CurrentWorkspace(WorkspaceOption):
    # The member's own record in this workspace; absent for a visiting platform administrator.
    user_id: UUID | None
    role_id: UUID | None
    role_name: str
    authorization_revision: int
    permissions: list[str]
    support: SupportVisit | None = None


class AccountView(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_platform_admin: bool
    two_step_enabled: bool
    recovery_codes_remaining: int


class MeView(BaseModel):
    account: AccountView
    session_id: UUID
    expires_at: datetime
    workspace: CurrentWorkspace | None
    next: NextStep


class MfaSetupView(BaseModel):
    secret: str = Field(repr=False)
    provisioning_uri: str = Field(repr=False)
    qr_image: str = Field(repr=False)
    expires_at: datetime


class RecoveryCodesView(BaseModel):
    codes: list[str] = Field(repr=False)


class InvitationDetails(BaseModel):
    workspace_name: str
    email: str
    display_name: str
    role_name: str
    expires_at: datetime
    password_required: bool


class InvitationView(BaseModel):
    id: UUID
    user_id: UUID
    expires_at: datetime
    delivery_status: str
    consumed: bool
