"""Safe building blocks for list filters."""

from sqlalchemy import ColumnElement, or_
from sqlalchemy.orm import InstrumentedAttribute


def contains(
    text: str | None, *columns: InstrumentedAttribute[str] | InstrumentedAttribute[str | None]
):
    """Case-insensitive "contains" across columns, or None when there is nothing to match.

    LIKE wildcards in the term are escaped, so a search for "50%" matches that text
    rather than everything.
    """
    term = (text or "").strip()
    if not term:
        return None
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conditions: list[ColumnElement[bool]] = [
        column.ilike(f"%{escaped}%", escape="\\") for column in columns
    ]
    return or_(*conditions)
