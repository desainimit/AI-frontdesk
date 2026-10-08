"""Application-defined section ownership. Permission names never come from tenant input."""

from dataclasses import dataclass
from typing import Literal

from app.auth.context import RequestContext
from app.auth.policies import Permission, has_permission, require_permission
from app.errors import ApplicationError, NotFoundError

ConfigurationSection = Literal[
    "business",
    "hours",
    "agent",
    "languages",
    "voice",
    "scheduling",
    "chat",
    "messaging",
    "workflows",
    "privacy",
    "notifications",
    "scopes",
]


@dataclass(frozen=True)
class SectionAuthority:
    read: Permission
    update: Permission
    publish: Permission


SCOPE_SECTION: dict[str, ConfigurationSection] = {
    "location": "hours",
    "agent": "agent",
    "channel": "voice",
}


SECTION_AUTHORITY: dict[str, SectionAuthority] = {
    **{
        key: SectionAuthority(
            Permission.TENANT_READ, Permission.TENANT_UPDATE, Permission.TENANT_PUBLISH
        )
        for key in ("business", "hours")
    },
    **{
        key: SectionAuthority(
            Permission.AGENT_READ, Permission.AGENT_UPDATE, Permission.AGENT_PUBLISH
        )
        for key in ("agent", "languages")
    },
    **{
        key: SectionAuthority(
            Permission.CHANNEL_READ, Permission.CHANNEL_UPDATE, Permission.CHANNEL_PUBLISH
        )
        for key in ("voice", "chat", "messaging")
    },
    "scheduling": SectionAuthority(
        Permission.SCHEDULING_READ, Permission.SCHEDULING_UPDATE, Permission.SCHEDULING_PUBLISH
    ),
    "workflows": SectionAuthority(
        Permission.WORKFLOW_READ, Permission.WORKFLOW_WRITE, Permission.WORKFLOW_PUBLISH
    ),
    "privacy": SectionAuthority(
        Permission.PRIVACY_READ, Permission.PRIVACY_UPDATE, Permission.PRIVACY_PUBLISH
    ),
    "notifications": SectionAuthority(
        Permission.NOTIFICATION_READ,
        Permission.NOTIFICATION_UPDATE,
        Permission.NOTIFICATION_PUBLISH,
    ),
    "scopes": SectionAuthority(
        Permission.CONFIGURATION_READ,
        Permission.CONFIGURATION_UPDATE,
        Permission.CONFIGURATION_PUBLISH,
    ),
}


def authority(
    section: str, action: Literal["read", "update", "publish"], context: RequestContext
) -> Permission:
    owner = SECTION_AUTHORITY.get(section)
    if owner is None:
        raise NotFoundError()
    broad = Permission(f"configuration:{action}")
    if has_permission(context, broad):
        return broad
    permission: Permission = getattr(owner, action)
    require_permission(context, permission)
    return permission


def in_publication_area(section: ConfigurationSection, field: str) -> bool:
    owner = SECTION_AUTHORITY[section]
    prefixes = [
        name for name, candidate in SECTION_AUTHORITY.items() if candidate.publish == owner.publish
    ]
    scope = {
        Permission.AGENT_PUBLISH: "scopes.agents",
        Permission.CHANNEL_PUBLISH: "scopes.channels",
        Permission.TENANT_PUBLISH: "scopes.locations",
    }.get(owner.publish)
    if scope:
        prefixes.append(scope)
    return any(field == prefix or field.startswith(prefix + ".") for prefix in prefixes)


class InvalidConfigurationSectionError(ApplicationError):
    status_code = 422
    code = "invalid_configuration"
    public_message = "These settings are invalid. Check the fields and supported constraints."


class SectionPublicationConflictError(ApplicationError):
    status_code = 409
    code = "section_publication_conflict"
    public_message = (
        "The draft includes changes outside this configuration area. Ask an authorized "
        "administrator to review and publish the complete draft."
    )


class InitialPublicationRequiredError(ApplicationError):
    status_code = 409
    code = "initial_publication_required"
    public_message = (
        "An authorized workspace administrator must review and publish "
        "the initial configuration first."
    )
