"""Whether a reply to "shall I book it?" is a yes, a no, or neither.

People rarely answer with a bare "yes": they say "haan, kar do", "uh-huh, go ahead",
"yes please, that works", or "हां कर दो, कोई बात नहीं" (where "नहीं" is part of "no
problem", not a refusal). A reply counts as yes only when every word in it is a word
of agreement or a filler around one; any refusal or change ("no", "मत करो", "change
it", "another time") makes it a no; anything else ("yes but tomorrow") is neither,
and the caller is asked again or the quicker model reads it. Extra content never
authorizes a change by itself.
"""

import unicodedata

from app.languages import all_packs

# Callers mix languages, so every language's words count, whatever the call's language.
_PACKS = all_packs()
# Said as agreement although they contain a word of refusal ("कोई बात नहीं", "no problem").
_AGREEMENT_IDIOMS = tuple(idiom for item in _PACKS for idiom in item.agreement_idioms)
_AGREE = frozenset[str]().union(*(item.agree for item in _PACKS))
# Words that may stand around a yes without changing it.
_FILLER = frozenset[str]().union(*(item.hesitation for item in _PACKS))
# A refusal or a change, in any language a caller may mix in.
_REFUSE = frozenset[str]().union(*(item.refuse for item in _PACKS))
# "na" ends a Hindi request politely ("kar do na"); anywhere else it is a refusal.
_SOFT_NO = frozenset[str]().union(*(item.soft_no for item in _PACKS))
# "No, that's not quite right": what a no to a read-back says beyond the no itself.
_NOT_RIGHT = frozenset[str]().union(*(item.not_right for item in _PACKS))


def _words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    for idiom in _AGREEMENT_IDIOMS:
        normalized = normalized.replace(idiom, " ok ")
    cleaned = "".join(" " if c in ',;:!?¡¿।."' else c for c in normalized)
    return [word.strip("'\u2019") for word in cleaned.split() if word.strip("'\u2019")]


def confirmation_answer(text: str, language: str = "en") -> bool | None:
    """True for a clear yes, False for a no or a change, None when it is neither.

    `language` is accepted for callers' clarity; callers mix languages, so every
    language's words count.
    """
    words = _words(text)
    if not words:
        return None
    for index, word in enumerate(words):
        if word in _REFUSE:
            return False
        if word in _SOFT_NO and index < len(words) - 1 and words[index + 1] not in {"ok"}:
            return False
    if not any(word in _AGREE for word in words):
        return None
    if all(word in _AGREE or word in _FILLER for word in words):
        return True
    return None


def plain_answer(text: str, language: str = "en") -> bool:
    """Only a yes or a no ("Yes yes please.", "No, thanks."), with nothing else in it."""
    return confirmation_answer(text, language) is True or only_a_no(text)


def only_a_no(text: str) -> bool:
    """A no and nothing more ("No, that's wrong"), not a correction ("No, it's Balsar")."""
    if confirmation_answer(text) is not False:
        return False
    plain = _REFUSE | _SOFT_NO | _FILLER | _NOT_RIGHT
    return all(word in plain for word in _words(text))
