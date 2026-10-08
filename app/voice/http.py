import math


def retry_after_seconds(value: str | None) -> float:
    """A bounded delay hint; HTTP-date or malformed values use a safe default."""
    try:
        delay = float(value or "5")
        return min(max(delay, 0), 60) if math.isfinite(delay) else 5
    except ValueError:
        return 5
