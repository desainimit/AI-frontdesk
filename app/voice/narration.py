"""What a receptionist says while looking something up: what they are checking, in
context, the way a person thinks aloud ("Let me see what Dr. Rao has on Friday").

It replaces a bare "one second": said as the lookup starts (if nothing has been said
yet this reply), varied, and never the same line twice in a call.
"""

from datetime import datetime
from typing import Any, cast

from app.ai.speech_time import spoken_day
from app.conversation.locales import normalize_language

# {doctor} and {day} are filled when the lookup names them; a line needing one that is
# not known is skipped.
LINES: dict[str, dict[str, tuple[str, ...]]] = {
    "check_availability": {
        "en": (
            "Let me see what {doctor} has {day}.",
            "Let me pull up {doctor}'s diary for {day}.",
            "Okay, let me check {day} for you.",
            "Let me see what's open {day}.",
            "Let me take a look at the diary.",
        ),
        "hi": (
            "चलिए, देखते हैं {day} {doctor} के पास क्या खाली है।",
            "जी, ज़रा देखते हैं {day} कौन सा समय खाली है।",
            "एक नज़र डालते हैं, {day} क्या मिल सकता है।",
            "जी, schedule देखते हैं।",
        ),
        "hinglish": (
            "Chaliye, dekhte hain {day} {doctor} ke paas kya khaali hai.",
            "Ji, zara dekhte hain {day} kaunsa time khaali hai.",
            "Ek nazar daalte hain, {day} kya mil sakta hai.",
            "Ji, schedule dekhte hain.",
        ),
        "gu": (
            "ચાલો, જોઈએ {day} {doctor} પાસે શું ખાલી છે.",
            "જી, જોઈએ {day} કયો સમય ખાલી છે.",
            "જી, શેડ્યૂલ જોઈએ.",
        ),
        "es": (
            "Déjeme ver qué tiene {doctor} {day}.",
            "Voy a mirar {day}.",
            "Déjeme ver la agenda.",
        ),
        "fr": ("Je regarde ce que {doctor} a {day}.", "Je regarde {day}.", "Je regarde l'agenda."),
        "de": (
            "Ich schaue, was {doctor} {day} frei hat.",
            "Ich schaue {day} nach.",
            "Ich schaue in den Kalender.",
        ),
        "pt": ("Vou ver o que {doctor} tem {day}.", "Vou olhar {day}.", "Vou ver a agenda."),
    },
    "list_my_appointments": {
        "en": ("Let me pull up your appointments.", "Let me have a look at your bookings."),
        "hi": ("जी, आपकी अपॉइंटमेंट्स देखते हैं।", "एक नज़र डालते हैं आपकी बुकिंग पर।"),
        "hinglish": (
            "Ji, aapki appointments dekhte hain.",
            "Ek nazar daalte hain aapki booking par.",
        ),
        "gu": ("જી, તમારી અપોઇન્ટમેન્ટ જોઈએ.",),
        "es": ("Déjeme ver sus citas.",),
        "fr": ("Je regarde vos rendez-vous.",),
        "de": ("Ich schaue mir Ihre Termine an.",),
        "pt": ("Vou ver suas consultas.",),
    },
    "book_appointment": {
        "en": ("Okay, let me set that up.", "Right, let me get that ready for you."),
        "hi": ("ठीक है, अभी सेट करते हैं।", "जी, तैयार करते हैं।"),
        "hinglish": ("Theek hai, abhi set karte hain.", "Ji, ready karte hain."),
        "gu": ("સારું, હમણાં ગોઠવીએ.",),
        "es": ("Muy bien, lo preparo.",),
        "fr": ("Très bien, je prépare ça.",),
        "de": ("Gut, ich bereite das vor.",),
        "pt": ("Certo, vou preparar.",),
    },
    "get_knowledge": {
        "en": ("Good question, let me check.", "Let me look that up for you."),
        "hi": ("अच्छा सवाल है, देखकर बताते हैं।", "जी, यह चेक करके बताते हैं।"),
        "hinglish": ("Achha sawaal hai, dekh kar batate hain.", "Ji, yeh check karke batate hain."),
        "gu": ("સારો સવાલ, જોઈને કહીએ.",),
        "es": ("Buena pregunta, déjeme mirar.",),
        "fr": ("Bonne question, je regarde.",),
        "de": ("Gute Frage, ich schaue nach.",),
        "pt": ("Boa pergunta, vou ver.",),
    },
    "register_patient": {
        "en": ("Let me just add you to our records.",),
        "hi": ("आपका नाम हमारे रिकॉर्ड में जोड़ते हैं।",),
        "hinglish": ("Aapka naam record mein jodte hain.",),
        "gu": ("તમારું નામ રેકોર્ડમાં ઉમેરીએ.",),
        "es": ("Le agrego a nuestros registros.",),
        "fr": ("Je vous ajoute à nos dossiers.",),
        "de": ("Ich nehme Sie kurz in unsere Kartei auf.",),
        "pt": ("Vou adicionar você ao nosso cadastro.",),
    },
    "create_ticket": {
        "en": ("Let me note that down for the team.",),
        "hi": ("यह टीम के लिए नोट कर रहे हैं।",),
        "hinglish": ("Yeh team ke liye note kar rahe hain.",),
        "gu": ("ટીમ માટે આ નોંધી લઈએ.",),
        "es": ("Lo anoto para el equipo.",),
        "fr": ("Je note ça pour l'équipe.",),
        "de": ("Ich notiere das für das Team.",),
        "pt": ("Vou anotar para a equipe.",),
    },
}
# Moving or cancelling sounds like booking: "let me set that up".
LINES["reschedule_appointment"] = LINES["book_appointment"]
LINES["cancel_appointment"] = LINES["book_appointment"]
for _directory in ("list_providers", "list_services", "list_locations"):
    LINES[_directory] = {
        "en": ("Let me check.",),
        "hi": ("जी, देखते हैं।",),
        "hinglish": ("Ji, dekhte hain.",),
        "gu": ("જી, જોઈએ.",),
        "es": ("Déjeme ver.",),
        "fr": ("Je regarde.",),
        "de": ("Ich schaue nach.",),
        "pt": ("Vou ver.",),
    }


def _doctor(arguments: dict[str, Any], directory: dict[str, Any] | None) -> str | None:
    wanted = str(arguments.get("provider_id") or "")
    doctors = cast(list[dict[str, Any]], (directory or {}).get("doctors") or [])
    for doctor in doctors:
        if str(doctor.get("id")) == wanted:
            return str(doctor.get("name") or "") or None
    return None


def _day(arguments: dict[str, Any], timezone: str, language: str) -> str | None:
    try:
        moment = datetime.fromisoformat(str(arguments.get("starts_at")))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return spoken_day(moment, timezone, language)


def narration(
    tool: str,
    arguments: dict[str, Any] | None,
    language: str,
    *,
    directory: dict[str, Any] | None,
    timezone: str,
    used: set[str],
) -> str | None:
    """The line to say as this lookup starts, or None when there is nothing fitting left."""
    lines = LINES.get(tool)
    if not lines:
        return None
    code = normalize_language(language)
    values = {
        "doctor": _doctor(arguments or {}, directory),
        "day": _day(arguments or {}, timezone, code),
    }
    for template in lines.get(code, lines["en"]):
        needed = [name for name in ("doctor", "day") if "{" + name + "}" in template]
        if any(values[name] is None for name in needed):
            continue
        line = template.format(**{name: values[name] for name in needed})
        if line not in used:
            used.add(line)
            return line
    return None
