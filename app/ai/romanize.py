"""Names in Latin letters, so a name said in English matches one stored in Hindi or
Gujarati ("Nimit Desai" and "निमित देसाई").

The Indian scripts share one Unicode layout (each block keeps the same letter at the
same offset), so one table serves Devanagari, Bengali, Gurmukhi, Gujarati, Oriya,
Tamil, Telugu, Kannada and Malayalam. It is close enough to compare names, not a
transliteration standard.
"""

import unicodedata

_BLOCKS = range(0x0900, 0x0D80)
_BLOCK = 0x80

_VOWELS = {
    0x05: "a", 0x06: "a", 0x07: "i", 0x08: "i", 0x09: "u", 0x0A: "u", 0x0B: "ri",
    0x0C: "li", 0x0D: "e", 0x0E: "e", 0x0F: "e", 0x10: "ai", 0x11: "o", 0x12: "o",
    0x13: "o", 0x14: "au", 0x60: "ri", 0x61: "li",
}  # fmt: skip
_SIGNS = {
    0x3E: "a", 0x3F: "i", 0x40: "i", 0x41: "u", 0x42: "u", 0x43: "ri", 0x44: "ri",
    0x45: "e", 0x46: "e", 0x47: "e", 0x48: "ai", 0x49: "o", 0x4A: "o", 0x4B: "o",
    0x4C: "au", 0x62: "li", 0x63: "li",
}  # fmt: skip
_CONSONANTS = {
    0x15: "k", 0x16: "kh", 0x17: "g", 0x18: "gh", 0x19: "n", 0x1A: "ch", 0x1B: "chh",
    0x1C: "j", 0x1D: "jh", 0x1E: "n", 0x1F: "t", 0x20: "th", 0x21: "d", 0x22: "dh",
    0x23: "n", 0x24: "t", 0x25: "th", 0x26: "d", 0x27: "dh", 0x28: "n", 0x29: "n",
    0x2A: "p", 0x2B: "ph", 0x2C: "b", 0x2D: "bh", 0x2E: "m", 0x2F: "y", 0x30: "r",
    0x31: "r", 0x32: "l", 0x33: "l", 0x34: "l", 0x35: "v", 0x36: "sh", 0x37: "sh",
    0x38: "s", 0x39: "h", 0x58: "q", 0x59: "kh", 0x5A: "g", 0x5B: "z", 0x5C: "r",
    0x5D: "rh", 0x5E: "f", 0x5F: "y",
}  # fmt: skip
_MARKS = {0x01: "n", 0x02: "n", 0x03: "h"}
_VIRAMA = 0x4D
_NUKTA = 0x3C


def romanize(text: str) -> str:
    """`text` with Indian-script letters in Latin ones; everything else as it was."""
    out: list[str] = []
    # A consonant carries an "a" unless a vowel sign or virama follows; at the end of
    # a word it is not said ("निमित" is "nimit").
    vowel_due = False
    for char in text:
        code = ord(char)
        offset = (code - _BLOCKS.start) % _BLOCK if code in _BLOCKS else None
        if offset is not None and offset == _NUKTA:
            continue
        if offset is not None and offset in _SIGNS:
            out.append(_SIGNS[offset])
            vowel_due = False
            continue
        if offset is not None and offset == _VIRAMA:
            vowel_due = False
            continue
        word_goes_on = offset is not None and (
            offset in _CONSONANTS or offset in _VOWELS or offset in _MARKS
        )
        if vowel_due and word_goes_on:
            out.append("a")
        vowel_due = False
        if offset is not None and offset in _CONSONANTS:
            out.append(_CONSONANTS[offset])
            vowel_due = True
        elif offset is not None and offset in _VOWELS:
            out.append(_VOWELS[offset])
        elif offset is not None and offset in _MARKS:
            out.append(_MARKS[offset])
        elif offset is not None:
            continue
        else:
            out.append(char)
    return "".join(out)


def latin_fold(text: str) -> str:
    """Romanized, lower case and without accents ("José" and "Jose" alike)."""
    decomposed = unicodedata.normalize("NFKD", romanize(unicodedata.normalize("NFC", text)))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


# Spellings of the same sound, made alike before names are compared ("Bhavesh" and
# "Bavesh", "Shah" and "Sah", "Vimal" and "Wimal").
_SOUNDS = (
    ("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"), ("w", "v"),
    ("ph", "f"), ("q", "k"), ("ck", "k"), ("z", "j"), ("kh", "k"), ("gh", "g"),
    ("chh", "c"), ("ch", "c"), ("jh", "j"), ("th", "t"), ("dh", "d"), ("bh", "b"),
    ("sh", "s"),
)  # fmt: skip


def sound_fold(word: str) -> str:
    """A folded word with spelling variants of one sound made alike and doubles single."""
    for spelling, sound in _SOUNDS:
        word = word.replace(spelling, sound)
    return "".join(c for i, c in enumerate(word) if i == 0 or c != word[i - 1])
