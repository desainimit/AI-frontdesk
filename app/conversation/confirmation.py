"""Exact localized consent phrases; these do not detect the caller's language."""

import re
import unicodedata

from app.conversation.locales import base_language

_AFFIRMATIVE = {
    "gu": {"હા", "હાં", "જી હા", "જી હાં", "હા કરો", "હા બુક કરો", "પુષ્ટિ કરું છું"},
    "en": {
        "yes",
        "yeah",
        "yep",
        "sure",
        "book it",
        "confirm",
        "please do",
        "ok",
        "okay",
        "proceed",
    },
    "es": {"sí", "si", "sí por favor", "si por favor", "confirmo", "confirmar"},
    "fr": {
        "oui",
        "oui merci",
        "oui sil vous plaît",
        "je confirme",
        "confirmez",
        "daccord",
    },
    "de": {"ja", "ja bitte", "bestätigen", "ich bestätige", "einverstanden"},
    "pt": {"sim", "sim por favor", "confirmo", "confirmar", "pode confirmar"},
    "hi": {
        "हाँ",
        "हां",
        "जी हाँ",
        "जी हां",
        "जी",
        "haan",
        "ha",
        "sahi hai",
        "theek hai",
        "karo",
        "kar dijiye",
        "bilkul",
        "zaroor",
    },
}
_NEGATIVE = {
    "gu": {"ના", "નહીં", "નહિ", "નથી", "રદ", "રદ્દ", "રોકો", "બદલો"},
    "en": {"no", "nope", "not", "dont", "never", "cancel", "stop", "nah", "change"},
    "es": {"no", "cancelar", "cancela", "pare"},
    "fr": {"non", "pas", "annuler", "annulez", "arrêtez"},
    "de": {"nein", "nicht", "abbrechen", "stopp"},
    "pt": {"não", "nao", "cancelar", "cancele", "pare"},
    "hi": {"नहीं", "नही", "मत", "रद्द", "nahi", "mat", "badlo"},
}


def confirmation_answer(text: str, language: str = "en") -> bool | None:
    # Preserve combining marks (including Hindi vowel signs) and accents.
    normalized = unicodedata.normalize("NFKC", text).casefold()
    cleaned = " ".join(
        "".join(" " if c in ",;:!?¡¿।" else c for c in normalized if c not in ".'\u2019\"").split()
    )
    code = base_language(language)
    # English acknowledgments are also common in multilingual calls. A complete
    # affirmative is still required; extra words never authorize a mutation.
    negative = _NEGATIVE.get(code, set()) | _NEGATIVE["en"] | _NEGATIVE["hi"] | _NEGATIVE["gu"]
    if any(word in negative for word in cleaned.split()):
        return False
    positive = _AFFIRMATIVE.get(code, set()) | _AFFIRMATIVE["en"]
    if cleaned in positive:
        return True
    if re.fullmatch(
        r"(?:yes|yeah|okay|ok)[ ]+(?:please[ ]+)?"
        r"(?:book (?:it|that)|confirm|proceed)(?: please)?",
        cleaned,
    ):
        return True
    return None
