from app.auth.policies import Permission

CUSTODIAN_PERMISSIONS = frozenset({Permission.USER_MANAGE, Permission.ROLE_UPDATE})
# The roles every new clinic starts with: (key, name, what it is for).
ROLE_TEMPLATES = (
    ("owner", "Owner", "Full access, including the team and going live."),
    ("admin", "Admin", "Runs the clinic: settings, schedule, knowledge and the team."),
    ("front_desk", "Front desk", "Day-to-day work: patients, appointments and calls."),
    ("viewer", "Viewer", "Can look at the schedule, calls and settings but not change them."),
)
_FRONT_DESK = (
    Permission.TENANT_READ,
    Permission.SETTINGS_READ,
    Permission.DIRECTORY_READ,
    Permission.PATIENT_READ,
    Permission.PATIENT_WRITE,
    Permission.APPOINTMENT_READ,
    Permission.APPOINTMENT_WRITE,
    Permission.CALL_READ,
    Permission.FOLLOW_UP_MANAGE,
    Permission.KNOWLEDGE_READ,
)
_VIEWER = (
    Permission.TENANT_READ,
    Permission.SETTINGS_READ,
    Permission.DIRECTORY_READ,
    Permission.PATIENT_READ,
    Permission.APPOINTMENT_READ,
    Permission.CALL_READ,
    Permission.KNOWLEDGE_READ,
)


def template_permissions(key: str) -> frozenset[str]:
    """Starting values only. Access always comes from the saved role."""
    if key == "owner":
        return frozenset(item.value for item in Permission)
    if key == "admin":
        return frozenset(item.value for item in Permission if item != Permission.TENANT_ACTIVATE)
    if key == "front_desk":
        return frozenset(item.value for item in _FRONT_DESK)
    if key == "viewer":
        return frozenset(item.value for item in _VIEWER)
    return frozenset()


def prerequisites(key: str) -> frozenset[str]:
    module, action = key.split(":", 1)
    read = f"{module}:read"
    known = {item.value for item in Permission}
    required: set[str] = (
        {Permission.TENANT_READ.value} if key != Permission.TENANT_READ.value else set()
    )
    if action != "read" and read in known:
        required.add(read)
    return frozenset(required)


_LABELS = {
    Permission.TENANT_READ: "Open the clinic workspace",
    Permission.TENANT_ACTIVATE: "Turn call answering on or off",
    Permission.SETTINGS_READ: "View settings",
    Permission.SETTINGS_UPDATE: "Change settings",
    Permission.DIRECTORY_READ: "View locations, providers and services",
    Permission.DIRECTORY_WRITE: "Change locations, providers, schedules and services",
    Permission.PATIENT_READ: "View patients",
    Permission.PATIENT_WRITE: "Add and edit patients",
    Permission.APPOINTMENT_READ: "View appointments",
    Permission.APPOINTMENT_WRITE: "Book, change and cancel appointments",
    Permission.CALL_READ: "View calls and transcripts",
    Permission.FOLLOW_UP_MANAGE: "Work on follow-ups",
    Permission.KNOWLEDGE_READ: "View knowledge",
    Permission.KNOWLEDGE_WRITE: "Add and approve knowledge",
    Permission.USER_READ: "View the team",
    Permission.USER_INVITE: "Invite team members",
    Permission.USER_MANAGE: "Deactivate and reactivate team members",
    Permission.ROLE_READ: "View roles",
    Permission.ROLE_CREATE: "Create roles",
    Permission.ROLE_UPDATE: "Change roles",
    Permission.ROLE_DELETE: "Delete roles",
    Permission.ROLE_ASSIGN: "Change a team member's role",
    Permission.INTEGRATION_READ: "View phone and provider status",
    Permission.INTEGRATION_MANAGE: "Manage phone and provider connections",
    Permission.AUDIT_READ: "View the activity log",
}
_GROUPS = {
    "tenant": "Clinic",
    "settings": "Clinic",
    "directory": "Schedule",
    "patient": "Schedule",
    "appointment": "Schedule",
    "call": "Conversations",
    "follow_up": "Conversations",
    "knowledge": "AI receptionist",
    "user": "Team",
    "role": "Team",
    "integration": "Advanced",
    "audit": "Advanced",
}


def permission_catalog() -> list[dict[str, object]]:
    return [
        {
            "key": item.value,
            "module": item.value.split(":")[0],
            "permission": item.value.split(":")[1],
            "group": _GROUPS[item.value.split(":")[0]],
            "label": _LABELS[item],
            "prerequisites": sorted(prerequisites(item.value)),
        }
        for item in Permission
    ]
