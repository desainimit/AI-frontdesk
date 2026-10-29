"""Server-owned caller messages in the conversation's stable language.

The words are in the language packs (app/languages/packs); every pack has every line.
"""

from typing import Any

from app.languages import CODES, pack

MESSAGES: dict[str, dict[str, str]] = {code: pack(code).messages for code in CODES}
# What a receptionist says while looking something up. Several, taken in turn, so the
# caller does not hear the same line every time.
FILLERS: dict[str, tuple[str, ...]] = {code: pack(code).fillers for code in CODES}


def filler(language: str, turn: int) -> str:
    choices = pack(language).fillers
    return choices[turn % len(choices)]


def spoken(language: str, key: str, **values: Any) -> str:
    text = pack(language).messages[key].format(**values)
    # "at 5 p.m." ending a sentence keeps one full stop.
    return text.replace(".m..", ".m.")
