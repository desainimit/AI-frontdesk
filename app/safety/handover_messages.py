"""What the receptionist says around a transfer: it always asks before putting anyone
through, and says plainly what happens next."""

from typing import Any

from app.languages import CODES, pack

# Hindi and Gujarati first-person verbs are gendered; the packs' lines avoid them.
HANDOVER: dict[str, dict[str, str]] = {code: pack(code).handover for code in CODES}


def handover_message(language: str, key: str, **values: Any) -> str:
    return pack(language).handover[key].format(**values)
