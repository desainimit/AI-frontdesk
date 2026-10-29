"""Short, localizable safety messages that do not depend on an LLM response."""

from app.conversation.state import SafetyDecision
from app.languages import CODES, pack
from app.safety.numbers import EmergencyLines

MESSAGES: dict[str, dict[str, str]] = {code: pack(code).safety for code in CODES}
# The same alarm with the number to call, where the clinic's country is known.
EMERGENCY_WITH_NUMBER: dict[str, str] = {code: pack(code).emergency_with_number for code in CODES}
# Someone thinking of harming themselves: kind and slow, a line they can call any time.
CRISIS: dict[str, str] = {code: pack(code).crisis for code in CODES}


def safety_message(
    language: str,
    decision: SafetyDecision,
    *,
    transfer_available: bool,
    lines: EmergencyLines | None = None,
    reason: str | None = None,
) -> str:
    messages = pack(language).safety
    if decision == SafetyDecision.EMERGENCY_PROTOCOL:
        return emergency_message(language, lines or EmergencyLines(), reason)
    if decision == SafetyDecision.CLARIFY:
        return messages["clarify"]
    if decision == SafetyDecision.TRANSFER and transfer_available:
        return messages["transfer"]
    return messages["unavailable"]


def transfer_failed_message(language: str) -> str:
    return pack(language).safety["transfer_failed"]


def emergency_message(language: str, lines: EmergencyLines, reason: str | None) -> str:
    """The alarm, with the numbers to call when the clinic's country is known."""
    words = pack(language)
    if reason == "self_harm" and lines.crisis and lines.emergency:
        return words.crisis.format(crisis=lines.crisis, number=lines.emergency)
    if lines.emergency:
        return words.emergency_with_number.format(number=lines.emergency)
    return words.safety["emergency"]
