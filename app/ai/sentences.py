"""Cut a reply into sentences as it arrives, so the first can be spoken at once."""

import re

# A sentence ends at ., !, ? or the Hindi danda, followed by a space. A full stop
# inside a number or right after a short abbreviation ("Dr.", "a.m.") is not an end.
_END = re.compile(r"([.!?।]+[\"')\]]*)(\s+)")
_ABBREVIATIONS = ("dr", "mr", "mrs", "ms", "st", "no", "vs", "a.m", "p.m", "e.g", "i.e")
MIN_CHARS = 12


class SentenceSplitter:
    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """Add text; get back the sentences that are now complete."""
        self._buffer += text
        sentences: list[str] = []
        start = 0
        for match in _END.finditer(self._buffer):
            candidate = self._buffer[start : match.end(1)].strip()
            last_word = (
                candidate.rstrip(".!?।\"')]").rsplit(None, 1)[-1].lower() if candidate else ""
            )
            if (
                len(candidate) < MIN_CHARS
                or last_word in _ABBREVIATIONS
                or (last_word.isdigit() and match.group(1) == ".")
            ):
                continue
            sentences.append(candidate)
            start = match.end()
        self._buffer = self._buffer[start:]
        return sentences

    def finish(self, *, cut_off: bool = False) -> str:
        """What is left at the end. A reply cut off mid-sentence loses the unfinished part."""
        rest, self._buffer = self._buffer.strip(), ""
        return "" if cut_off else rest
