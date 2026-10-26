"""Cut a reply into sentences as it arrives, so the first can be spoken at once."""

import re

# A sentence ends at ., !, ? or the Hindi danda, followed by a space. A full stop
# inside a number or right after a short abbreviation ("Dr.", "a.m.") is not an end.
_END = re.compile(r"([.!?।]+[\"')\]]*)(\s+)")
_ABBREVIATIONS = ("dr", "mr", "mrs", "ms", "st", "no", "vs", "a.m", "p.m", "e.g", "i.e")
MIN_CHARS = 12
# On a call, a long first sentence is spoken from its first comma on, so the voice starts
# while the rest is still being written: "I've got Tuesday at ten or Thursday at two," ...
_CLAUSE = re.compile(r"([,;:—])(\s+)")
FIRST_CLAUSE_CHARS = 24


class SentenceSplitter:
    def __init__(self, *, first_clause: bool = False) -> None:
        self._buffer = ""
        self._first_clause = first_clause

    def feed(self, text: str) -> list[str]:
        """Add text; get back the sentences that are now complete."""
        self._buffer += text
        sentences: list[str] = []
        if self._first_clause:
            clause = self._clause()
            if clause:
                sentences.append(clause)
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

    def _clause(self) -> str:
        """The first clause of the first sentence, once it is long enough to say alone."""
        if _END.search(self._buffer):
            # A whole sentence is already here: it goes out as one.
            self._first_clause = False
            return ""
        for match in _CLAUSE.finditer(self._buffer):
            clause = self._buffer[: match.end(1)].strip()
            if len(clause) >= FIRST_CLAUSE_CHARS:
                self._first_clause = False
                self._buffer = self._buffer[match.end() :]
                return clause
        return ""

    def finish(self, *, cut_off: bool = False) -> str:
        """What is left at the end. A reply cut off mid-sentence loses the unfinished part."""
        rest, self._buffer = self._buffer.strip(), ""
        return "" if cut_off else rest
