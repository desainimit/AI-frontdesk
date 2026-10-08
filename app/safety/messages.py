"""Short, localizable safety messages that do not depend on an LLM response."""

# ruff: noqa: E501

from app.conversation.state import SafetyDecision

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "emergency": "This may be an emergency. Call your local emergency services now. I cannot provide emergency care over this call.",
        "clarify": "Are you having trouble breathing, chest pain, or another urgent symptom right now?",
        "transfer": "I will connect you to a staff member now.",
        "unavailable": "I cannot connect you to staff from this call. Please contact the business directly.",
        "transfer_failed": "I could not connect you to staff. Please contact the business directly.",
    },
    "es": {
        "emergency": "Esto puede ser una emergencia. Llame ahora a los servicios de emergencia locales. No puedo brindar atención de emergencia por esta llamada.",
        "clarify": "¿Tiene dificultad para respirar, dolor en el pecho u otro síntoma urgente ahora mismo?",
        "transfer": "Ahora le conectaré con una persona del equipo.",
        "unavailable": "No puedo conectarle con el equipo desde esta llamada. Contacte directamente con el negocio.",
        "transfer_failed": "No pude conectarle con el equipo. Contacte directamente con el negocio.",
    },
    "fr": {
        "emergency": "Il peut s'agir d'une urgence. Appelez maintenant les secours locaux. Je ne peux pas fournir de soins d'urgence pendant cet appel.",
        "clarify": "Avez-vous du mal à respirer, une douleur à la poitrine ou un autre symptôme urgent en ce moment ?",
        "transfer": "Je vais vous mettre en relation avec un membre de l'équipe.",
        "unavailable": "Je ne peux pas vous mettre en relation avec l'équipe depuis cet appel. Contactez directement l'établissement.",
        "transfer_failed": "Je n'ai pas pu vous mettre en relation avec l'équipe. Contactez directement l'établissement.",
    },
    "de": {
        "emergency": "Das könnte ein Notfall sein. Rufen Sie jetzt den örtlichen Rettungsdienst an. Ich kann in diesem Anruf keine Notfallversorgung leisten.",
        "clarify": "Haben Sie gerade Atemnot, Brustschmerzen oder ein anderes dringendes Symptom?",
        "transfer": "Ich verbinde Sie jetzt mit einer Person aus dem Team.",
        "unavailable": "Ich kann Sie in diesem Anruf nicht mit dem Team verbinden. Kontaktieren Sie den Betrieb bitte direkt.",
        "transfer_failed": "Die Verbindung zum Team ist fehlgeschlagen. Kontaktieren Sie den Betrieb bitte direkt.",
    },
    "pt": {
        "emergency": "Isso pode ser uma emergência. Ligue agora para o serviço de emergência local. Não posso prestar atendimento de emergência nesta ligação.",
        "clarify": "Você está com dificuldade para respirar, dor no peito ou outro sintoma urgente agora?",
        "transfer": "Vou conectar você a alguém da equipe agora.",
        "unavailable": "Não consigo conectar você à equipe nesta ligação. Entre em contato diretamente com o estabelecimento.",
        "transfer_failed": "Não consegui conectar você à equipe. Entre em contato diretamente com o estabelecimento.",
    },
    "hi": {
        "emergency": "यह आपात स्थिति हो सकती है। अभी अपनी स्थानीय आपातकालीन सेवा को कॉल करें। मैं इस कॉल पर आपातकालीन इलाज नहीं दे सकता।",
        "clarify": "क्या आपको अभी सांस लेने में दिक्कत, सीने में दर्द या कोई और गंभीर लक्षण है?",
        "transfer": "मैं अभी आपकी बात किसी कर्मचारी से कराता हूँ।",
        "unavailable": "मैं इस कॉल से आपको कर्मचारी से नहीं जोड़ सकता। कृपया व्यवसाय से सीधे संपर्क करें।",
        "transfer_failed": "कर्मचारी से कॉल नहीं जुड़ सकी। कृपया व्यवसाय से सीधे संपर्क करें।",
    },
    "hinglish": {
        "emergency": "Yeh emergency ho sakti hai. Abhi apni local emergency service ko call karein. Main is call par emergency care nahi de sakta.",
        "clarify": "Kya aapko abhi saans lene mein dikkat, seene mein dard, ya koi aur serious symptom hai?",
        "transfer": "Main abhi aapko staff se connect karta hoon.",
        "unavailable": "Is call se staff se connect nahi kar sakta. Kripya business se seedha sampark karein.",
        "transfer_failed": "Staff se call connect nahi ho saki. Kripya business se seedha sampark karein.",
    },
}


def safety_message(language: str, decision: SafetyDecision, *, transfer_available: bool) -> str:
    messages = MESSAGES.get(language, MESSAGES["en"])
    if decision == SafetyDecision.EMERGENCY_PROTOCOL:
        return messages["emergency"]
    if decision == SafetyDecision.CLARIFY:
        return messages["clarify"]
    if decision == SafetyDecision.TRANSFER and transfer_available:
        return messages["transfer"]
    return messages["unavailable"]


def transfer_failed_message(language: str) -> str:
    return MESSAGES.get(language, MESSAGES["en"])["transfer_failed"]
