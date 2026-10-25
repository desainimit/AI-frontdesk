"""Remove verification material before keeping caller text or sending it to models."""

import re

_ACCESS_CODE = re.compile(r"(?<!\d)\d{8}(?!\d)")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE_LIKE = re.compile(r"(?<!\w)\+?\d[\d(). -]{7,}\d(?!\w)")
_ACCOUNT_IDENTIFIER = re.compile(
    r"(?i)\b(?:account\s*(?:id|number|identifier)|"
    r"identificador\s+de\s+cuenta|num[eé]ro\s+de\s+compte|"
    r"kontonummer|identificador\s+da\s+conta|ખાતા(?:નો)?\s*(?:નંબર|ક્રમાંક))"
    r"\s*(?::|=|is|es|est|ist|é|છે)?\s*[A-Za-z\d._-]{4,64}"
)
_DATE_OF_BIRTH = re.compile(
    r"(?i)\b(?:dob|date\s+of\s+birth|fecha\s+de\s+nacimiento|"
    r"date\s+de\s+naissance|geburtsdatum|data\s+de\s+nascimento|જન્મ\s*તારીખ)"
    r"\s*(?::|=|is|es|est|ist|é|છે)?\s*"
    r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"
)


def redact_verification_secrets(text: str) -> str:
    text = _DATE_OF_BIRTH.sub("[date of birth]", text)
    text = _ACCOUNT_IDENTIFIER.sub("[account identifier]", text)
    return _ACCESS_CODE.sub("[call access code]", text)


def redact_transcript(text: str) -> str:
    """Redact common identifiers before a bounded transcript leaves call memory."""
    clean = _EMAIL.sub("[email]", redact_verification_secrets(text))

    def replace_phone(match: re.Match[str]) -> str:
        return "[phone number]" if sum(c.isdigit() for c in match.group()) >= 9 else match.group()

    return _PHONE_LIKE.sub(
        replace_phone,
        clean,
    )
