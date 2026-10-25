"""Normalize speech transcripts while preserving Unicode letters and vowel marks."""

import re
import unicodedata


def normalize_transcript(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = re.sub(r"\b(can'?t|cannot)\b", "cannot", value)
    value = re.sub(r"\b(don'?t)\b", "do not", value)
    value = re.sub(r"\bwon'?t\b", "will not", value)
    value = re.sub(r"\bisn'?t\b", "is not", value)
    value = re.sub(r"\bcan t\b", "cannot", value)
    value = "".join(
        char
        if char.isspace() or char == "'" or unicodedata.category(char)[0] in {"L", "M", "N"}
        else " "
        for char in value
    )
    return " ".join(value.split())[:4000]
