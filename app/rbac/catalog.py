from app.auth.policies import Permission

CATALOG_VERSION = 2
CUSTODIAN_PERMISSIONS = frozenset({Permission.USER_MANAGE, Permission.ROLE_UPDATE})


def template_permissions(key: str) -> frozenset[str]:
    """Seed values only. Effective access always comes from persisted role records."""
    if key in {"owner", "admin"}:
        return frozenset(item.value for item in Permission)
    if key == "operator":
        return frozenset(
            item.value
            for item in (
                Permission.TENANT_READ,
                Permission.DIRECTORY_READ,
                Permission.KNOWLEDGE_READ,
                Permission.CALL_READ,
                Permission.PATIENT_READ,
                Permission.PATIENT_WRITE,
                Permission.APPOINTMENT_READ,
                Permission.APPOINTMENT_WRITE,
                Permission.BUSINESS_READ,
                Permission.CONVERSATION_READ,
                Permission.CONVERSATION_WRITE,
                Permission.TICKET_READ,
                Permission.TICKET_WRITE,
            )
        )
    if key == "analyst":
        return frozenset({Permission.TENANT_READ.value, Permission.AUDIT_READ.value})
    if key == "member":
        return frozenset(
            {
                Permission.TENANT_READ.value,
                Permission.DIRECTORY_READ.value,
                Permission.KNOWLEDGE_READ.value,
            }
        )
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


def permission_catalog() -> list[dict[str, object]]:
    return [
        {
            "key": item.value,
            "module": item.value.split(":")[0],
            "permission": item.value.split(":")[1],
            "label": item.value.replace(":", " ").replace("_", " ").title(),
            "prerequisites": sorted(prerequisites(item.value)),
        }
        for item in Permission
    ]
