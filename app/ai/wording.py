"""Server-owned caller messages in the conversation's stable language."""
# ruff: noqa: E501

from typing import Any

MESSAGES: dict[str, dict[str, str]] = {
    "gu": {
        "access_denied": "પ્રવેશની પુષ્ટિ થઈ શકી નથી. કૃપા કરીને રિસેપ્શનનો સંપર્ક કરો.",
        "identified": "આ કૉલ માટે રિસેપ્શન દ્વારા મંજૂર કરાયેલ પ્રવેશ સક્રિય છે.",
        "identity_required": "ઓળખની ચકાસણી પછી કૃપા કરીને રિસેપ્શન પાસેથી કૉલ માટેનો પ્રવેશ કોડ મેળવો.",
        "slot_unavailable": "તે સમય હવે ઉપલબ્ધ નથી. કૃપા કરીને બીજો સમય પસંદ કરો.",
        "appointment_unavailable": "તે મુલાકાતમાં ફેરફાર કરી શકાય તેમ નથી. કૃપા કરીને રિસેપ્શનનો સંપર્ક કરો.",
        "new_slot_unavailable": "નવો સમય ઉપલબ્ધ નથી. કૃપા કરીને બીજો સમય પસંદ કરો.",
        "book_offer": "તો {time}, {provider} સાથે. બુક કરી દઉં?",
        "cancel_offer": "ખાતરી કરી લઉં: {time}ની {provider} સાથેની મુલાકાત રદ કરી દઉં?",
        "move_offer": "તો {old}થી બદલીને {new} કરી દઉં, {provider} સાથે. બરાબર?",
        "confirm": "{offer}",
        "discarded": "કોઈ વાંધો નહીં, હજુ કંઈ બદલ્યું નથી. કહો, તમે શું ઇચ્છો છો?",
        "booked": "થઈ ગયું, તમારી મુલાકાત {time} માટે બુક થઈ ગઈ છે. તમારો અપોઇન્ટમેન્ટ નંબર {reference} છે.",
        "cancelled": "થઈ ગયું, તે મુલાકાત રદ કરી દીધી છે.",
        "moved": "થઈ ગયું, મુલાકાત {time} પર કરી દીધી છે. અપોઇન્ટમેન્ટ નંબર એ જ {reference} છે.",
        "unavailable": "અત્યારે તમારી વિનંતી પૂરી કરી શકાય તેમ નથી. કૃપા કરીને રિસેપ્શનનો સંપર્ક કરો.",
        "knowledge_unavailable": "મને તે વિશે મંજૂર કરેલી માહિતી મળી નથી.",
        "one_moment": "કૃપા કરીને એક ક્ષણ રાહ જુઓ.",
        "staff_will_reply": "હું આ કામ પૂરું કરી શક્યો નથી. તમારી વિનંતી ટીમને મોકલી છે. તેઓ ટૂંક સમયમાં તમારો સંપર્ક કરશે.",
        "staff_will_call": "અત્યારે હું મદદ કરી શકતો નથી. તમને પાછો કૉલ કરવા માટે ટીમને સંદેશ મોકલ્યો છે.",
        "could_not_answer": "હું તેનો જવાબ આપી શક્યો નથી. શું ટીમ માટે તમારો સંદેશ લઈ લઉં?",
        "say_again": "માફ કરશો, શું તમે ફરી કહી શકશો?",
        "line_unclear": "માફ કરશો, અવાજ થોડો કપાઈ ગયો. શું તમે ફરી કહી શકશો?",
        "still_there": "હેલો? શું તમે લાઇન પર છો?",
        "goodbye_silent": "હાલ પૂરતો કૉલ મૂકીએ છીએ. કૉલ કરવા બદલ આભાર, ગમે ત્યારે ફરી કૉલ કરજો. તમારો દિવસ શુભ રહે!",
        "closed_now": "અત્યારે ક્લિનિક બંધ છે, પણ એપોઇન્ટમેન્ટ અને પ્રશ્નોમાં મદદ થઈ શકે છે.",
        "hello_name": "નમસ્તે {name}!",
        "test_mode": "આ પરીક્ષણ માટેની વાતચીત છે, તેથી કોઈ ફેરફાર થયો નથી. વાસ્તવિક વાતચીતમાં હું હવે પુષ્ટિ માગીશ.",
        "stopped": "તમને હવે અમારા તરફથી સંદેશા નહીં મળે. ફરી શરૂ કરવા માટે START લખીને જવાબ આપો.",
        "started": "સંદેશા ફરી શરૂ થયા છે. અમે કેવી રીતે મદદ કરી શકીએ?",
    },
    "en": {
        "access_denied": "Access could not be confirmed. Please contact reception.",
        "identified": "Reception-authorized access is active for this call.",
        "identity_required": "Please obtain a call access code from reception after verification.",
        "slot_unavailable": "Ah, that time's just been taken. Want to pick another one?",
        "appointment_unavailable": "Sorry, I can't change that one myself. The front desk can sort it out for you.",
        "new_slot_unavailable": "Ah, that new time's gone. Want to pick another one?",
        "book_offer": "Okay, so that's {provider} {time}. Shall I book it for you?",
        "cancel_offer": "Just to check: you'd like to cancel your appointment {time} with {provider}?",
        "move_offer": "So that's {provider} {new}, instead of {old}. Is that okay?",
        "confirm": "{offer}",
        "discarded": "No problem, I haven't changed anything. What would work better?",
        "booked": "Done, you're all booked {time}. Your appointment number is {reference}.",
        "cancelled": "Done, that appointment is cancelled.",
        "moved": "Done, it's moved. You're now booked {time}, and your appointment number stays {reference}.",
        "unavailable": "Sorry, I can't do that one from here right now. The front desk can help you with it.",
        "knowledge_unavailable": "Hmm, I don't have that info in front of me.",
        "one_moment": "One moment, please.",
        "staff_will_reply": "I couldn't finish that one myself, so I've passed it to the team. Someone will get back to you soon.",
        "staff_will_call": "Sorry, I can't sort that out right now, but I've left a note for the team to call you back.",
        "could_not_answer": "Hmm, I don't have an answer for that one. Want me to have someone call you back?",
        "say_again": "Sorry, could you say that again?",
        "line_unclear": "Sorry, I missed that. Could you say it again?",
        "still_there": "Hello? You still there?",
        "goodbye_silent": "Okay, I'll let you go for now. Call back any time, and have a lovely day!",
        "closed_now": "Just so you know, we're closed right now, but I can still help with appointments and questions.",
        "hello_name": "Hi {name}!",
        "test_mode": "This is a test chat, so nothing was changed. In a real conversation I would now ask for confirmation.",
        "stopped": "You will not get any more texts from us. Reply START to turn them back on.",
        "started": "Texts are back on. How can we help?",
    },
    "es": {
        "one_moment": "Un momento, por favor.",
        "staff_will_reply": "No he podido completarlo. Lo he pasado al equipo y le responderán en breve.",
        "say_again": "Perdone, ¿puede repetirlo?",
        "line_unclear": "Perdone, se cortó un momento. ¿Me lo repite?",
        "still_there": "¿Hola? ¿Sigue ahí?",
        "goodbye_silent": "Le dejo por ahora. Gracias por llamar, llámenos cuando quiera. ¡Que tenga un buen día!",
        "closed_now": "Ahora estamos cerrados, pero puedo ayudarle con citas y preguntas.",
        "hello_name": "¡Hola, {name}!",
        "access_denied": "No se pudo confirmar el acceso. Contacte con recepción.",
        "identified": "El acceso autorizado por recepción está activo para esta llamada.",
        "identity_required": (
            "Solicite un código de acceso para la llamada en recepción después de la verificación."
        ),
        "slot_unavailable": "Ese horario ya no está disponible. Elija otro.",
        "appointment_unavailable": "No se puede cambiar esa cita. Contacte con recepción.",
        "new_slot_unavailable": "El nuevo horario no está disponible. Elija otro.",
        "book_offer": "Entonces sería {time} con {provider}. ¿Se la reservo?",
        "cancel_offer": "Solo para confirmar: ¿cancelo su cita {time} con {provider}?",
        "move_offer": "Entonces la paso de {old} a {new} con {provider}. ¿Le parece bien?",
        "confirm": "{offer}",
        "discarded": "He descartado esa propuesta. ¿Qué desea cambiar?",
        "booked": "Listo, su cita es {time}. Su número de cita es {reference}.",
        "cancelled": "Su cita ha sido cancelada.",
        "moved": "Listo, la he cambiado a {time}. Su número de cita sigue siendo {reference}.",
        "unavailable": "No puedo completar esa solicitud ahora. Contacte con recepción.",
        "knowledge_unavailable": "No encontré información aprobada sobre eso.",
    },
    "fr": {
        "one_moment": "Un instant, s'il vous plaît.",
        "staff_will_reply": "Je n'ai pas pu terminer. J'ai transmis votre demande à l'équipe, qui vous répondra bientôt.",
        "say_again": "Pardon, pouvez-vous répéter ?",
        "line_unclear": "Pardon, la ligne a coupé un instant. Vous pouvez répéter ?",
        "still_there": "Allô ? Vous êtes toujours là ?",
        "goodbye_silent": "Je vous laisse pour le moment. Merci de votre appel, rappelez quand vous voulez. Bonne journée !",
        "closed_now": "Nous sommes fermés pour le moment, mais je peux vous aider pour les rendez-vous et les questions.",
        "hello_name": "Bonjour {name} !",
        "access_denied": "L'accès n'a pas pu être confirmé. Contactez l'accueil.",
        "identified": "L'accès autorisé par l'accueil est actif pour cet appel.",
        "identity_required": (
            "Demandez un code d'accès pour cet appel à l'accueil après vérification."
        ),
        "slot_unavailable": "Ce créneau n'est plus disponible. Choisissez une autre heure.",
        "appointment_unavailable": "Ce rendez-vous ne peut pas être modifié. Contactez l'accueil.",
        "new_slot_unavailable": "Le nouveau créneau est indisponible. Choisissez une autre heure.",
        "book_offer": "Donc {time} avec {provider}. Je vous le réserve ?",
        "cancel_offer": "Juste pour être sûr : j'annule votre rendez-vous {time} avec {provider} ?",
        "move_offer": "Donc je le déplace de {old} à {new} avec {provider}. Ça vous va ?",
        "confirm": "{offer}",
        "discarded": "J'ai annulé cette proposition. Que souhaitez-vous modifier ?",
        "booked": "C'est fait, votre rendez-vous est {time}. Votre numéro de rendez-vous est {reference}.",
        "cancelled": "Votre rendez-vous a été annulé.",
        "moved": "C'est fait, je l'ai déplacé {time}. Votre numéro reste {reference}.",
        "unavailable": "Je ne peux pas traiter cette demande maintenant. Contactez l'accueil.",
        "knowledge_unavailable": "Je n'ai pas trouvé d'information approuvée à ce sujet.",
    },
    "de": {
        "one_moment": "Einen Moment bitte.",
        "staff_will_reply": "Das konnte ich nicht abschließen. Ich habe es an das Team weitergegeben; es meldet sich in Kürze.",
        "say_again": "Entschuldigung, können Sie das wiederholen?",
        "line_unclear": "Entschuldigung, die Leitung war kurz weg. Können Sie das wiederholen?",
        "still_there": "Hallo? Sind Sie noch dran?",
        "goodbye_silent": "Ich lege jetzt auf. Danke für Ihren Anruf, rufen Sie gern jederzeit wieder an. Einen schönen Tag noch!",
        "closed_now": "Wir haben gerade geschlossen, aber ich helfe Ihnen gern bei Terminen und Fragen.",
        "hello_name": "Hallo {name}!",
        "access_denied": (
            "Der Zugriff konnte nicht bestätigt werden. Bitte wenden Sie sich an den Empfang."
        ),
        "identified": "Der vom Empfang genehmigte Zugriff ist für diesen Anruf aktiv.",
        "identity_required": (
            "Bitte lassen Sie sich nach der Überprüfung vom Empfang einen Zugangscode geben."
        ),
        "slot_unavailable": (
            "Dieser Termin ist nicht mehr verfügbar. Bitte wählen Sie eine andere Uhrzeit."
        ),
        "appointment_unavailable": (
            "Dieser Termin kann nicht geändert werden. Bitte wenden Sie sich an den Empfang."
        ),
        "new_slot_unavailable": (
            "Der neue Termin ist nicht verfügbar. Bitte wählen Sie eine andere Uhrzeit."
        ),
        "book_offer": "Also {time} bei {provider}. Soll ich das für Sie buchen?",
        "cancel_offer": "Nur zur Sicherheit: Soll ich Ihren Termin {time} bei {provider} absagen?",
        "move_offer": "Dann verschiebe ich ihn von {old} auf {new} bei {provider}. Passt das?",
        "confirm": "{offer}",
        "discarded": "Ich habe diesen Vorschlag verworfen. Was möchten Sie ändern?",
        "booked": "Erledigt, Ihr Termin ist {time}. Ihre Terminnummer ist {reference}.",
        "cancelled": "Ihr Termin wurde abgesagt.",
        "moved": "Erledigt, ich habe ihn auf {time} verschoben. Ihre Terminnummer bleibt {reference}.",
        "unavailable": (
            "Ich kann diese Anfrage gerade nicht bearbeiten. Bitte wenden Sie sich an den Empfang."
        ),
        "knowledge_unavailable": "Dazu habe ich keine freigegebenen Informationen gefunden.",
    },
    "pt": {
        "one_moment": "Um momento, por favor.",
        "staff_will_reply": "Não consegui concluir. Passei o pedido à equipa, que responderá em breve.",
        "say_again": "Desculpe, pode repetir?",
        "line_unclear": "Desculpe, a ligação falhou um instante. Pode repetir?",
        "still_there": "Alô? Ainda está aí?",
        "goodbye_silent": "Vou encerrar por agora. Obrigado por ligar, ligue quando quiser. Tenha um ótimo dia!",
        "closed_now": "Estamos fechados agora, mas posso ajudar com consultas e dúvidas.",
        "hello_name": "Olá, {name}!",
        "access_denied": "Não foi possível confirmar o acesso. Entre em contato com a recepção.",
        "identified": "O acesso autorizado pela recepção está ativo para esta ligação.",
        "identity_required": (
            "Após a verificação, solicite à recepção um código de acesso para esta ligação."
        ),
        "slot_unavailable": "Esse horário não está mais disponível. Escolha outro.",
        "appointment_unavailable": (
            "Não é possível alterar esse agendamento. Entre em contato com a recepção."
        ),
        "new_slot_unavailable": "O novo horário não está disponível. Escolha outro.",
        "book_offer": "Então seria {time} com {provider}. Posso agendar?",
        "cancel_offer": "Só para confirmar: cancelo sua consulta {time} com {provider}?",
        "move_offer": "Então mudo de {old} para {new} com {provider}. Tudo bem?",
        "confirm": "{offer}",
        "discarded": "Descartei essa proposta. O que deseja alterar?",
        "booked": "Pronto, sua consulta é {time}. O número da consulta é {reference}.",
        "cancelled": "Seu agendamento foi cancelado.",
        "moved": "Pronto, mudei para {time}. O número da consulta continua {reference}.",
        "unavailable": (
            "Não posso concluir essa solicitação agora. Entre em contato com a recepção."
        ),
        "knowledge_unavailable": "Não encontrei informações aprovadas sobre isso.",
    },
    "hi": {
        "one_moment": "एक क्षण रुकिए।",
        "staff_will_reply": "मैं यह पूरा नहीं कर सका। मैंने आपका अनुरोध टीम को भेज दिया है, वे जल्द ही आपसे संपर्क करेंगे।",
        "say_again": "माफ़ कीजिए, क्या आप दोबारा कह सकते हैं?",
        "line_unclear": "माफ़ कीजिए, आवाज़ थोड़ी कट गई। क्या आप दोबारा बोल सकते हैं?",
        "still_there": "हैलो? क्या आप लाइन पर हैं?",
        "goodbye_silent": "अभी के लिए कॉल रख रहे हैं। कॉल करने के लिए धन्यवाद, कभी भी दोबारा कॉल करें। आपका दिन शुभ हो!",
        "closed_now": "अभी क्लिनिक बंद है, पर अपॉइंटमेंट और सवालों में मदद हो सकती है।",
        "hello_name": "नमस्ते {name} जी!",
        "access_denied": "पहचान की पुष्टि नहीं हो सकी। कृपया रिसेप्शन से संपर्क करें।",
        "identified": "इस कॉल के लिए रिसेप्शन द्वारा अनुमत पहुँच सक्रिय है।",
        "identity_required": "जाँच के बाद कृपया रिसेप्शन से कॉल एक्सेस कोड लें।",
        "slot_unavailable": "यह समय अब उपलब्ध नहीं है। कृपया दूसरा समय चुनें।",
        "appointment_unavailable": ("इस अपॉइंटमेंट को बदला नहीं जा सकता। कृपया रिसेप्शन से संपर्क करें।"),
        "new_slot_unavailable": "नया समय उपलब्ध नहीं है। कृपया दूसरा समय चुनें।",
        "book_offer": "तो {time}, {provider} के साथ। क्या मैं यह बुक कर दूँ?",
        "cancel_offer": "बस पक्का कर लूँ: {time} वाली {provider} की अपॉइंटमेंट रद्द कर दूँ?",
        "move_offer": "तो {old} से बदलकर {new} कर दूँ, {provider} के साथ। ठीक है?",
        "confirm": "{offer}",
        "discarded": "कोई बात नहीं, अभी कुछ नहीं बदला। बताइए, आप क्या चाहेंगे?",
        "booked": "हो गया, आपकी अपॉइंटमेंट {time} के लिए बुक हो गई है। आपका अपॉइंटमेंट नंबर {reference} है।",
        "cancelled": "हो गया, वह अपॉइंटमेंट रद्द कर दी है।",
        "moved": "हो गया, अपॉइंटमेंट {time} पर कर दी है। अपॉइंटमेंट नंबर वही {reference} है।",
        "unavailable": "अभी यह अनुरोध पूरा नहीं हो सकता। कृपया रिसेप्शन से संपर्क करें।",
        "knowledge_unavailable": "मुझे इस बारे में स्वीकृत जानकारी नहीं मिली।",
    },
    "hinglish": {
        "one_moment": "Ek minute rukiye.",
        "staff_will_reply": "Main yeh complete nahi kar paya. Maine aapki request team ko bhej di hai, woh jaldi aapse contact karenge.",
        "say_again": "Sorry, kya aap dobara bol sakte hain?",
        "line_unclear": "Sorry, awaaz thodi kat gayi. Kya aap dobara bol sakte hain?",
        "still_there": "Hello? Kya aap line par hain?",
        "goodbye_silent": "Abhi ke liye call rakh rahe hain. Call karne ke liye dhanyavaad, kabhi bhi dobara call karein. Aapka din shubh ho!",
        "closed_now": "Abhi clinic band hai, par appointment aur sawaalon mein madad ho sakti hai.",
        "hello_name": "Namaste {name} ji!",
        "access_denied": "Access confirm nahi ho saka. Kripya reception se baat karein.",
        "identified": "Is call ke liye reception-authorized access active hai.",
        "identity_required": "Verification ke baad reception se call access code lein.",
        "slot_unavailable": "Yeh slot ab available nahi hai. Kripya doosra time chunein.",
        "appointment_unavailable": (
            "Yeh appointment change nahi ho sakti. Reception se baat karein."
        ),
        "new_slot_unavailable": "Naya slot available nahi hai. Kripya doosra time chunein.",
        "book_offer": "Toh {time}, {provider} ke saath. Kya main book kar doon?",
        "cancel_offer": "Bas confirm kar loon: {time} wali {provider} ki appointment cancel kar doon?",
        "move_offer": "Toh {old} se badalkar {new} kar doon, {provider} ke saath. Theek hai?",
        "confirm": "{offer}",
        "discarded": "Koi baat nahi, abhi kuch nahi badla. Bataiye, aap kya chahenge?",
        "booked": "Ho gaya, aapki appointment {time} ke liye book ho gayi hai. Aapka appointment number {reference} hai.",
        "cancelled": "Ho gaya, woh appointment cancel kar di hai.",
        "moved": "Ho gaya, appointment {time} par kar di hai. Appointment number wahi {reference} hai.",
        "unavailable": "Abhi yeh request poori nahi ho sakti. Reception se baat karein.",
        "knowledge_unavailable": "Is baare mein approved information nahi mili.",
    },
}


# What a receptionist says while looking something up. Several, taken in turn, so the
# caller does not hear the same line every time.
FILLERS: dict[str, tuple[str, ...]] = {
    # A soft acknowledgement for a long pause; never "one second".
    "en": ("Let's see.", "Okay, bear with me.", "Hmm, let me see.", "Almost there."),
    "es": ("Déjeme ver.", "Muy bien, a ver.", "Claro, lo miro."),
    "fr": ("Je regarde.", "D'accord, voyons.", "Bien sûr, je vérifie."),
    "de": ("Ich schaue kurz nach.", "Gut, mal sehen.", "Klar, ich prüfe das."),
    "pt": ("Deixe-me ver.", "Certo, vamos ver.", "Claro, vou verificar."),
    # Hindi first-person verbs are gendered; these lines avoid them.
    "hi": ("जी, देखते हैं।", "अच्छा, ठीक है।", "हम्म, समझ गए।"),
    "hinglish": ("Ji, dekhte hain.", "Achha, theek hai.", "Hmm, samajh gaye."),
    "gu": ("જી, જોઈએ.", "સારું, બરાબર.", "હમ્મ, સમજાયું."),
}


def filler(language: str, turn: int) -> str:
    choices = FILLERS.get(language, FILLERS["en"])
    return choices[turn % len(choices)]


def spoken(language: str, key: str, **values: Any) -> str:
    templates = MESSAGES.get(language, MESSAGES["en"])
    # A sentence not yet translated is said in English rather than not at all.
    text = (templates.get(key) or MESSAGES["en"][key]).format(**values)
    # "at 5 p.m." ending a sentence keeps one full stop.
    return text.replace(".m..", ".m.")
