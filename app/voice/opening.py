"""What the caller hears first, in the order a receptionist says it.

"Hi Nimit!" for a caller the clinic knows, then the clinic's greeting, then that the
clinic is closed, and the greeting's question ("How can I help?") last, so the call
opens with a question the caller can answer rather than a notice.
"""

import re
from dataclasses import dataclass

# The greeting's last sentence, when it is a question with something before it.
_LAST_QUESTION = re.compile(r"^(?P<body>.*[.!।])\s+(?P<question>[^.!?।]+[??])\s*$", re.DOTALL)


@dataclass(frozen=True)
class Line:
    text: str
    # Kept in the transcript; "Hi Nimit!" is not, so the model does not greet again.
    record: bool = True
    # The same on every call: made once, then replayed.
    cache: bool = True


def opening(greeting: str, *, hello: str | None, closed: str | None) -> list[Line]:
    lines = [Line(hello, record=False, cache=False)] if hello else []
    if not closed:
        return [*lines, Line(greeting)]
    match = _LAST_QUESTION.match(greeting.strip())
    if match is None:
        return [*lines, Line(greeting), Line(closed)]
    return [*lines, Line(match["body"]), Line(closed), Line(match["question"])]
