"""A reply that says "let me check" has promised the caller a lookup.

A model sometimes says it ("मैं अभी देख लेती हूँ") and stops without looking anything
up, so the caller hears "let me check" and then silence. The server notices the
promise and has the model keep it.
"""

import re

_PROMISE = re.compile(
    "|".join(
        (
            # English
            r"\b(let me|i'll|i will|i'm going to|let's)\s+(just\s+|quickly\s+)?"
            r"(check|see|look|pull up|find|have a look|take a look)\b",
            # Hindi
            r"देख\s?(ती|ता|ते|ूँ|ूं|\s?ले)",
            r"चेक\s?कर",
            r"पता\s?कर",
            # Hinglish
            r"\bdekh\s?(ti|ta|te|leti|leta|lete|lu|loon)\b",
            r"\bcheck\s?kar",
            r"\bpata\s?kar",
            # Gujarati ("જોઈએ" alone also means "need", so it is left out)
            r"જોઈ\s?(લઉં|લઈએ|લઉ)",
            r"જોઉં",
            r"તપાસ",
            r"ચેક\s?કર",
            # Spanish, French, German, Portuguese
            r"\b(d[ée]jeme ver|voy a (mirar|ver|revisar))\b",
            r"\bje (regarde|v[ée]rifie)\b",
            r"\bich (schaue|sehe|pr[üu]fe)\b",
            r"\b(vou (ver|olhar|verificar)|deixa eu ver)\b",
        )
    ),
    re.IGNORECASE,
)


def promises_a_lookup(text: str) -> bool:
    return bool(_PROMISE.search(text))
