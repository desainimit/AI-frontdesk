"""Tool identifiers independent of runtime and persistence imports."""

SAFE_READ_TOOLS = frozenset(
    {"list_locations", "list_providers", "list_services", "list_staff", "get_knowledge"}
)
APPOINTMENT_TOOLS = frozenset(
    {
        "identify_patient",
        "get_providers",
        "check_availability",
        "list_my_appointments",
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
    }
)
ALL_TOOLS = SAFE_READ_TOOLS | APPOINTMENT_TOOLS
MUTATION_TOOLS = frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"})
