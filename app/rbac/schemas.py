from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.auth.policies import Permission
from app.rbac.catalog import prerequisites


class PermissionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    module: str = Field(pattern=r"^[a-z_]+$", max_length=40)
    permission: str = Field(pattern=r"^[a-z_]+$", max_length=40)
    allowed: bool

    @property
    def key(self) -> str:
        return f"{self.module}:{self.permission}"


class RoleCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    permissions: list[PermissionGrant] = Field(max_length=len(Permission))

    @model_validator(mode="after")
    def validate_permissions(self) -> "RoleCommand":
        known = {item.value for item in Permission}
        keys = [grant.key for grant in self.permissions]
        allowed = {grant.key for grant in self.permissions if grant.allowed}
        if len(keys) != len(set(keys)) or not set(keys) <= known:
            raise ValueError("Permissions must be unique registered module/action pairs")
        if any(not prerequisites(key) <= allowed for key in allowed):
            raise ValueError("Enable prerequisite read permissions explicitly")
        return self


class UpdateRole(RoleCommand):
    revision: int = Field(ge=1, strict=True)


class AssignRole(BaseModel):
    role_id: UUID


class RoleOption(BaseModel):
    id: UUID
    name: str


class RoleView(BaseModel):
    id: UUID
    name: str
    description: str
    revision: int
    permissions: list[PermissionGrant]


class CapabilitiesView(BaseModel):
    tenant_id: UUID
    actor_id: UUID | None
    role_id: UUID | None
    role_name: str
    authorization_revision: int
    permissions: list[Permission]
