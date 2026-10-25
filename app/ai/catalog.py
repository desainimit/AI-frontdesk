"""Tool identifiers independent of runtime and persistence imports."""

SAFE_READ_TOOLS = frozenset({"list_locations", "list_providers", "list_services", "get_knowledge"})
APPOINTMENT_TOOLS = frozenset(
    {
        "identify_patient",
        "register_patient",
        "check_availability",
        "list_my_appointments",
        "book_appointment",
        "cancel_appointment",
        "reschedule_appointment",
    }
)
# Always available: leave a request for the team, or ask for a person.
SUPPORT_TOOLS = frozenset({"create_ticket", "transfer_to_staff"})
# Only on a call.
VOICE_TOOLS = frozenset({"end_call"})
ALL_TOOLS = SAFE_READ_TOOLS | APPOINTMENT_TOOLS | SUPPORT_TOOLS | VOICE_TOOLS
MUTATION_TOOLS = frozenset({"book_appointment", "cancel_appointment", "reschedule_appointment"})
