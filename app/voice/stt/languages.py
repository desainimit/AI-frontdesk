"""Parse Deepgram's documented word labels and alternative languages."""

from collections import Counter
from typing import Any, cast

from app.conversation.locales import normalize_language


def language_counts(channel: dict[str, Any], alternative: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    words = alternative.get("words", [])
    if isinstance(words, list):
        for word in cast(list[Any], words):
            if isinstance(word, dict):
                code = cast(dict[str, Any], word).get("language")
                if isinstance(code, str) and code.strip() and code != "multi":
                    counts[normalize_language(code)] += 1
    if counts:
        return counts
    languages = alternative.get("languages", [])
    if isinstance(languages, list):
        for index, code in enumerate(cast(list[Any], languages)):
            if isinstance(code, str) and code.strip() and code != "multi":
                counts[normalize_language(code)] += len(cast(list[Any], languages)) - index
    detected = channel.get("detected_language")
    if not counts and isinstance(detected, str) and detected.strip() and detected != "multi":
        counts[normalize_language(detected)] = 1
    return counts
