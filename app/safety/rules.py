"""Explicit high-confidence safety rules; the classifier can add, never veto, alarms."""

# ruff: noqa: E501

import re
from dataclasses import dataclass

from app.conversation.state import SafetyDecision
from app.safety.normalization import normalize_transcript


@dataclass(frozen=True)
class RuleResult:
    decision: SafetyDecision
    reason: str
    context_hint: str = "none"


# These are risk indicators, not a language detector or a medical diagnosis.
EMERGENCY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (code, re.compile(pattern, re.IGNORECASE))
    for code, pattern in (
        (
            "breathing",
            r"\b(?:cannot|can not|unable to|not|stopped|struggling to|hard to|difficulty)\s+breath(?:e|ing)?\b|"
            r"\b(?:trouble breathing|short of breath|cannot catch my breath|"
            r"cannot get air|breathless|choking|suffocating)\b|\b(?:saans|sans)\s+(?:nahi|nahin|nai)\s+(?:aa|a)\s+rahi\b|"
            r"\b(?:saans|sans)\s+(?:ruk|band|nahi\s+le\s+pa)\b|"
            r"\b(?:saans|sans)\s+lene\s+(?:me|mein)\s+dikkat\b|"
            r"(?:सांस|साँस)\s*(?:नहीं\s*आ|रुक|बंद|लेने\s*में\s*दिक्कत)|दम\s*घुट|"
            r"શ્વાસ\s*(?:લેવામાં\s*(?:તકલીફ|મુશ્કેલી)|લેવાતો\s*નથી|નથી\s*(?:આવતો|લેવાતો)|બંધ)|ગૂંગળામણ|"
            r"\b(?:no puedo respirar|no puede respirar|je ne peux pas respirer|"
            r"ich kann nicht atmen|não consigo respirar)\b",
        ),
        (
            "chest",
            r"\b(?:chest pain|pain in (?:(?:my|the|his|her) )?chest|"
            r"chest (?:hurts|is hurting|is tight)|"
            r"chest (?:feels? |is |bahut |very )?(?:tight|heavy|pressure)|"
            r"pressure in (?:my|the|his|her) chest|heart attack|cardiac arrest)\b|\b(?:seene|sine|chati|chaati)\s+(?:mein|me)\s+(?:dard|pain)\b|"
            r"(?:सीने|छाती)\s*में\s*(?:दर्द|जकड़न)|दिल\s*का\s*दौरा|"
            r"છાતી(?:માં|મા)?\s*(?:દુખાવો|દર્દ|દબાણ)|હૃદયરોગનો\s*હુમલો|હાર્ટ\s*એટેક|"
            r"\b(?:dolor de pecho|douleur thoracique|brustschmerzen|dor no peito)\b",
        ),
        (
            "neurologic",
            r"\b(?:having a stroke|has a stroke|having a seizure|is unconscious|"
            r"passed out and (?:not|is not) waking|collapsed and (?:not|is not) waking|"
            r"unconscious|seizing|face (?:is )?droop\w*|"
            r"(?:he|she|they|my father|my mother) (?:has )?collapsed)\b|"
            r"\b(?:behosh|behosh ho gaya|behosh hai)\b|बेहोश|બેભાન|ખેંચ\s*આવી|"
            r"\b(?:accidente cerebrovascular|attaque cérébrale|schlaganfall|derrame cerebral)\b",
        ),
        (
            "bleeding",
            r"\b(?:severe bleeding|bleeding (?:won t|will not|does not) stop|"
            r"a lot of blood|bleeding heavily)\b|\b(?:bahut khoon beh raha|khoon nahi ruk raha)\b|"
            r"खून\s*(?:नहीं\s*रुक|बहुत\s*बह)|"
            r"(?:લોહી|રક્ત)\s*(?:વહેવાનું\s*બંધ\s*થતું\s*નથી|બંધ\s*થતું\s*નથી)|ખૂબ\s*(?:લોહી|રક્ત)\s*વહી|"
            r"\b(?:sangrado intenso|saignement abondant|starke blutung|sangramento intenso)\b",
        ),
        (
            "overdose",
            r"\b(?:overdos(?:e|ed)|took too many (?:pills|tablets|medicines)|"
            r"swallowed a bottle of pills|swallowed poison)\b|\b(?:bahut (?:sari|saari) goliyan kha)\b|"
            r"बहुत\s*(?:गोलियां|गोलियाँ)\s*खा|વધુ\s*પડતી\s*(?:ગોળીઓ|દવા)\s*(?:લીધી|ખાધી)|ઝેર\s*પીધું",
        ),
        (
            "self_harm",
            r"\b(?:kill myself|end my life|want to die|about to hurt myself|"
            r"suicidal|going to jump|want to hurt myself)\b|\b(?:khudkushi|aatmahatya)\b|आत्महत्या|खुदकुशी|"
            r"આત્મહત્યા|મારે\s*મરી\s*જવું\s*છે|પોતાને\s*(?:મારી\s*નાખ|નુકસાન\s*કર)",
        ),
    )
)
HUMAN_REQUEST = re.compile(
    r"\b(?:speak|talk|connect|transfer)\s+(?:me\s+)?(?:to|with)\s+(?:(?:a|the)\s+)?"
    r"(?:human|person|receptionist|staff|manager|agent)\b|"
    r"\b(?:insaan|reception|receptionist)\s+se\s+baat\b|"
    r"(?:इंसान|रिसेप्शन|रिसेप्शनिस्ट)\s*से\s*बात|"
    r"(?:સ્ટાફ|રિસેપ્શન|રિસેપ્શનિસ્ટ|વ્યક્તિ|માણસ)\s*સાથે\s*(?:વાત|જોડો)|"
    r"\b(?:hablar con una persona|parler à une personne|mit einem menschen sprechen|"
    r"falar com uma pessoa)\b",
    re.IGNORECASE,
)
AMBIGUOUS = re.compile(
    r"\b(?:chest|breath\w*|saans|sans|seene|sine|chaati|chati|dizzy|faint|"
    r"severe pain)\b|(?:सांस|साँस|सीने|छाती|बेहोश)|શ્વાસ|છાતી|ચક્કર|સખત\s*દુખાવો",
    re.IGNORECASE,
)
HISTORICAL = re.compile(
    r"^(?:last year|years ago|yesterday|previously|used to|what if|"
    r"in a movie|in a book|do you treat|appointment for|history of)\b|"
    r"\b(?:i|he|she|we|they|my father|my mother|my husband|my wife)\s+"
    r"(?:had|felt|experienced)\b.{0,100}\b(?:yesterday|last year|years ago)\b|"
    r"\b(?:have|has)\s+(?:a\s+)?history of\b|^(?:ગયા\s*વર્ષે|ગઈકાલે|પહેલાં)",
    re.IGNORECASE,
)
CURRENT = re.compile(r"\b(?:now|today|currently|right now|abhi|ab|aaj)\b|अभी|आज|અત્યારે|હમણાં|આજે")
INFORMATIONAL = re.compile(
    r"^(?:what is|what are|tell me about|explain|how do you treat|do you treat)\b|(?:શું\s*છે|વિશે\s*જણાવો)",
    re.IGNORECASE,
)
SCHEDULING = re.compile(r"\b(?:appointment|schedule|book|x ray|xray|clinic)\b|મુલાકાત|બુક|ક્લિનિક")
SYMPTOM = re.compile(
    r"\b(?:pain|hurt|hurts|tight|heavy|strange|worse|dard|dikkat)\b|दर्द|દુખાવો|દર્દ|તકલીફ"
)

NEGATED_PREFIX = re.compile(
    r"(?:\bno|\bnot|\bwithout|\bdo not have|\bnever had|\bdenies|\bnegative for|"
    r"\bnahi|\bnahin|\bnai|नहीं)\s+(?:\w+\s+){0,2}$",
    re.IGNORECASE,
)
WORSENING = re.compile(
    r"\b(?:getting worse|worsening|much worse|more severe|abhi zyada|badhta ja raha)\b|"
    r"(?:बढ़\s*रहा|और\s*बुरा)|(?:વધી\s*રહ|વધારે\s*ખરાબ|વધુ\s*દુખાવો)",
    re.IGNORECASE,
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
        and re.search(
            r"\b(?:i|me|my|he|she|we|myself)\b|મને|મારો|મારી|મારા|અમને|પિતા|માતા",
            normalized,
        )
        is None
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
            if re.search(r"\b(?:chest|seene|sine|chaati|chati)\b|सीने|छाती|છાતી", normalized):
                hint = "chest"
            elif re.search(r"\b(?:breath\w*|saans|sans)\b|सांस|साँस|શ્વાસ", normalized):
                hint = "breathing"
            return RuleResult(SafetyDecision.CLARIFY, "ambiguous_symptom", hint)
    return RuleResult(SafetyDecision.CONTINUE, "none")
