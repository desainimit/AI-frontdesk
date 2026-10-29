"""What a receptionist says while looking something up: what they are checking, in
context, the way a person thinks aloud ("Let me see what Dr. Rao has on Friday").

It replaces a bare "one second": said as the lookup starts (if nothing has been said
yet this reply), varied, and never the same line twice in a call.
"""

from datetime import datetime
from typing import Any, cast

from app.ai.speech_time import spoken_day
from app.languages import CODES, normalize, pack

# {doctor} and {day} are filled when the lookup names them; a line needing one that is
# not known is skipped. Tool -> language -> lines, from the language packs.
LINES: dict[str, dict[str, tuple[str, ...]]] = {
    tool: {code: pack(code).narration[tool] for code in CODES} for tool in pack("en").narration
}


def _doctor(arguments: dict[str, Any], directory: dict[str, Any] | None) -> str | None:
    wanted = str(arguments.get("provider_id") or "")
    doctors = cast(list[dict[str, Any]], (directory or {}).get("doctors") or [])
    for doctor in doctors:
        if str(doctor.get("id")) == wanted:
            return str(doctor.get("name") or "") or None
    return None


def _day(arguments: dict[str, Any], timezone: str, language: str) -> str | None:
    try:
        moment = datetime.fromisoformat(str(arguments.get("starts_at")))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return spoken_day(moment, timezone, language)


def narration(
    tool: str,
    arguments: dict[str, Any] | None,
    language: str,
    *,
    directory: dict[str, Any] | None,
    timezone: str,
    used: set[str],
) -> str | None:
    """The line to say as this lookup starts, or None when there is nothing fitting left."""
    lines = LINES.get(tool)
    if not lines:
        return None
    code = normalize(language)
    values = {
        "doctor": _doctor(arguments or {}, directory),
        "day": _day(arguments or {}, timezone, code),
    }
    for template in lines.get(code, lines["en"]):
        needed = [name for name in ("doctor", "day") if "{" + name + "}" in template]
        if any(values[name] is None for name in needed):
            continue
        line = template.format(**{name: values[name] for name in needed})
        if line not in used:
            used.add(line)
            return line
    return None
