"""Explicit high-confidence safety rules; the classifier can add, never veto, alarms."""

import re
from dataclasses import dataclass

from app.conversation.state import SafetyDecision
from app.languages import all_packs
from app.safety.normalization import normalize_transcript


@dataclass(frozen=True)
class RuleResult:
    decision: SafetyDecision
    reason: str
    context_hint: str = "none"


# These are risk indicators, not a language detector or a medical diagnosis. Each is
# every language pack's way of saying it (app/languages/packs, SafetyWords): a caller
# may say it in any language, whatever the call's language.
_WORDS = tuple(item.safety_words for item in all_packs())


def _any(category: str, flags: int = re.IGNORECASE) -> re.Pattern[str]:
    return re.compile("|".join(getattr(words, category) for words in _WORDS), flags)


EMERGENCY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, _any(code))
    for code in ("breathing", "chest", "neurologic", "bleeding", "overdose", "self_harm")
)
HUMAN_REQUEST = _any("human_request")
AMBIGUOUS = _any("ambiguous")
HISTORICAL = _any("historical")
CURRENT = _any("current", 0)
INFORMATIONAL = _any("informational")
SCHEDULING = _any("scheduling", 0)
SYMPTOM = _any("symptom", 0)
NEGATED_PREFIX = re.compile(
    "(?:" + "|".join(words.negation for words in _WORDS) + r")\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE,
)
WORSENING = _any("worsening")
# Someone speaking of themselves or their family: then "what is chest pain" is not a
# general question.
_PERSONAL = re.compile(
    r"\b(?:i|me|my|he|she|we|myself|yo|mi|él|ella|je|moi|mon|ma|il|elle|ich|mich|mein|meine"
    r"|er|eu|meu|minha|ele|ela|mujhe|mera|meri|main)\b"
    r"|मुझे|मेरा|मेरी|मैं|મને|મારો|મારી|મારા|અમને|પિતા|માતા",
    re.IGNORECASE,
)
_CHEST = re.compile(
    r"\b(?:chest|seene|sine|chaati|chati|pecho|poitrine|brust|peito)\b|सीने|छाती|છાતી",
    re.IGNORECASE,
)
_BREATH = re.compile(
    r"\b(?:breath\w*|saans|sans|respirar|respirer|atmen)\b|सांस|साँस|શ્વાસ", re.IGNORECASE
)


def _gujarati_denial(text: str, match: re.Match[str]) -> bool:
    return (
        any("\u0a80" <= char <= "\u0aff" for char in match.group())
        and re.match(
            r"(?:માં|મા)?\s*(?:(?:દુખાવો|દર્દ|દબાણ|તકલીફ|થતો|થતી|થતું|આવે)\s*){0,3}"
            r"(?:નથી|નહીં|નહિ)(?:\s|$)",
            text[match.end() :],
        )
        is not None
    )


def deterministic_rule(text: str, previous_hint: str = "none") -> RuleResult:
    normalized = normalize_transcript(text)
    if not normalized:
        return RuleResult(SafetyDecision.CONTINUE, "empty")
    historical = HISTORICAL.search(normalized)
    current = CURRENT.search(normalized)
    informational = (
        INFORMATIONAL.search(normalized) is not None
        and CURRENT.search(normalized) is None
        and _PERSONAL.search(normalized) is None
    )
    for code, pattern in EMERGENCY_RULES:
        for match in pattern.finditer(normalized):
            prefix = normalized[max(0, match.start() - 35) : match.start()]
            past_match = (
                historical is not None
                and match.start() >= historical.start()
                and (current is None or match.start() < current.start())
            )
            if past_match or informational or NEGATED_PREFIX.search(prefix):
                continue
            # Gujarati places a denial after the symptom. Do not confuse this with
            # inability to breathe or bleeding that will not stop inside the match.
            if _gujarati_denial(normalized, match):
                continue
            return RuleResult(SafetyDecision.EMERGENCY_PROTOCOL, code, code)
    human_request = HUMAN_REQUEST.search(normalized)
    if human_request and not _gujarati_denial(normalized, human_request):
        return RuleResult(SafetyDecision.TRANSFER, "human_requested")
    if previous_hint in {"chest", "breathing"} and WORSENING.search(normalized):
        return RuleResult(SafetyDecision.EMERGENCY_PROTOCOL, "symptoms_worsening", previous_hint)
    if (
        not historical
        and not informational
        and not (SCHEDULING.search(normalized) and not SYMPTOM.search(normalized))
    ):
        for match in AMBIGUOUS.finditer(normalized):
            prefix = normalized[max(0, match.start() - 35) : match.start()]
            if NEGATED_PREFIX.search(prefix) or _gujarati_denial(normalized, match):
                continue
            hint = "other"
            if _CHEST.search(normalized):
                hint = "chest"
            elif _BREATH.search(normalized):
                hint = "breathing"
            return RuleResult(SafetyDecision.CLARIFY, "ambiguous_symptom", hint)
    return RuleResult(SafetyDecision.CONTINUE, "none")
