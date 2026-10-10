"""Every list answers with ``{items, next_cursor}``; the cursor is opaque to callers."""

import base64
import binascii
from collections.abc import Sequence

from pydantic import BaseModel

MAX_OFFSET = 100_000


class Page[T](BaseModel):
    items: list[T]
    # Send back as ``cursor`` to get the next page; null on the last one.
    next_cursor: str | None = None


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """Where a page starts. A missing or unreadable cursor starts from the beginning."""
    if not cursor:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        offset = int(text.removeprefix("o:"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return 0
    return offset if 0 <= offset <= MAX_OFFSET else 0


def page[T](rows: Sequence[T], limit: int, offset: int) -> Page[T]:
    """Build a page from ``limit + 1`` fetched rows."""
    return Page(
        items=list(rows[:limit]),
        next_cursor=encode_cursor(offset + limit) if len(rows) > limit else None,
    )
