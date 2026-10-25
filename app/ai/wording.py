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
        "book_offer": "શું તમારી મુલાકાત {time} માટે બુક કરું?",
        "cancel_offer": "શું તમારી {time}ની મુલાકાત રદ કરું?",
        "move_offer": "શું તમારી મુલાકાત {old}થી બદલીને {new} માટે રાખું?",
        "confirm": "{provider}. {offer} પુષ્ટિ કરવા માટે હા કહો.",
        "discarded": "તે પ્રસ્તાવ દૂર કર્યો છે. તમે શું બદલવા માંગો છો?",
        "booked": "તમારી મુલાકાત {time} માટે બુક થઈ ગઈ છે.",
        "cancelled": "તમારી મુલાકાત રદ થઈ ગઈ છે.",
        "moved": "તમારી મુલાકાતનો સમય બદલીને {time} કર્યો છે.",
        "unavailable": "અત્યારે તમારી વિનંતી પૂરી કરી શકાય તેમ નથી. કૃપા કરીને રિસેપ્શનનો સંપર્ક કરો.",
        "knowledge_unavailable": "મને તે વિશે મંજૂર કરેલી માહિતી મળી નથી.",
        "one_moment": "કૃપા કરીને એક ક્ષણ રાહ જુઓ.",
        "staff_will_reply": "હું આ કામ પૂરું કરી શક્યો નથી. તમારી વિનંતી ટીમને મોકલી છે. તેઓ ટૂંક સમયમાં તમારો સંપર્ક કરશે.",
        "staff_will_call": "અત્યારે હું મદદ કરી શકતો નથી. તમને પાછો કૉલ કરવા માટે ટીમને સંદેશ મોકલ્યો છે.",
        "could_not_answer": "હું તેનો જવાબ આપી શક્યો નથી. શું ટીમ માટે તમારો સંદેશ લઈ લઉં?",
        "say_again": "માફ કરશો, શું તમે ફરી કહી શકશો?",
        "test_mode": "આ પરીક્ષણ માટેની વાતચીત છે, તેથી કોઈ ફેરફાર થયો નથી. વાસ્તવિક વાતચીતમાં હું હવે પુષ્ટિ માગીશ.",
        "stopped": "તમને હવે અમારા તરફથી સંદેશા નહીં મળે. ફરી શરૂ કરવા માટે START લખીને જવાબ આપો.",
        "started": "સંદેશા ફરી શરૂ થયા છે. અમે કેવી રીતે મદદ કરી શકીએ?",
    },
    "en": {
        "access_denied": "Access could not be confirmed. Please contact reception.",
        "identified": "Reception-authorized access is active for this call.",
        "identity_required": "Please obtain a call access code from reception after verification.",
        "slot_unavailable": "That slot is no longer available. Please choose another time.",
        "appointment_unavailable": "That appointment cannot be changed. Please contact reception.",
        "new_slot_unavailable": "That new slot is unavailable. Please choose another time.",
        "book_offer": "Book your appointment at {time}?",
        "cancel_offer": "Cancel your appointment at {time}?",
        "move_offer": "Move your appointment from {old} to {new}?",
        "confirm": "{provider}. {offer} Say yes to confirm.",
        "discarded": "I have discarded that proposal. What would you like to change?",
        "booked": "Your appointment is booked for {time}.",
        "cancelled": "Your appointment has been cancelled.",
        "moved": "Your appointment has been moved to {time}.",
        "unavailable": "I cannot complete that request right now. Please contact reception.",
        "knowledge_unavailable": "I couldn't find approved information about that.",
        "one_moment": "One moment, please.",
        "staff_will_reply": "I could not finish that myself. I have passed it to the team and someone will get back to you shortly.",
        "staff_will_call": "I'm sorry, I cannot help with that right now. I have left a note for the team to call you back.",
        "could_not_answer": "I'm sorry, I could not answer that. Would you like me to take a message for the team?",
        "say_again": "Sorry, could you say that again?",
        "test_mode": "This is a test chat, so nothing was changed. In a real conversation I would now ask for confirmation.",
        "stopped": "You will not get any more texts from us. Reply START to turn them back on.",
        "started": "Texts are back on. How can we help?",
    },
    "es": {
        "one_moment": "Un momento, por favor.",
        "staff_will_reply": "No he podido completarlo. Lo he pasado al equipo y le responderán en breve.",
        "say_again": "Perdone, ¿puede repetirlo?",
        "access_denied": "No se pudo confirmar el acceso. Contacte con recepción.",
        "identified": "El acceso autorizado por recepción está activo para esta llamada.",
        "identity_required": (
            "Solicite un código de acceso para la llamada en recepción después de la verificación."
        ),
        "slot_unavailable": "Ese horario ya no está disponible. Elija otro.",
        "appointment_unavailable": "No se puede cambiar esa cita. Contacte con recepción.",
        "new_slot_unavailable": "El nuevo horario no está disponible. Elija otro.",
        "book_offer": "¿Reservo su cita para {time}?",
        "cancel_offer": "¿Cancelo su cita de {time}?",
        "move_offer": "¿Cambio su cita de {old} a {new}?",
        "confirm": "{provider}. {offer} Diga sí para confirmar.",
        "discarded": "He descartado esa propuesta. ¿Qué desea cambiar?",
        "booked": "Su cita está reservada para {time}.",
        "cancelled": "Su cita ha sido cancelada.",
        "moved": "Su cita se ha cambiado a {time}.",
        "unavailable": "No puedo completar esa solicitud ahora. Contacte con recepción.",
        "knowledge_unavailable": "No encontré información aprobada sobre eso.",
    },
    "fr": {
        "one_moment": "Un instant, s'il vous plaît.",
        "staff_will_reply": "Je n'ai pas pu terminer. J'ai transmis votre demande à l'équipe, qui vous répondra bientôt.",
        "say_again": "Pardon, pouvez-vous répéter ?",
        "access_denied": "L'accès n'a pas pu être confirmé. Contactez l'accueil.",
        "identified": "L'accès autorisé par l'accueil est actif pour cet appel.",
        "identity_required": (
            "Demandez un code d'accès pour cet appel à l'accueil après vérification."
        ),
        "slot_unavailable": "Ce créneau n'est plus disponible. Choisissez une autre heure.",
        "appointment_unavailable": "Ce rendez-vous ne peut pas être modifié. Contactez l'accueil.",
        "new_slot_unavailable": "Le nouveau créneau est indisponible. Choisissez une autre heure.",
        "book_offer": "Dois-je réserver votre rendez-vous pour {time} ?",
        "cancel_offer": "Dois-je annuler votre rendez-vous de {time} ?",
        "move_offer": "Dois-je déplacer votre rendez-vous de {old} à {new} ?",
        "confirm": "{provider}. {offer} Dites oui pour confirmer.",
        "discarded": "J'ai annulé cette proposition. Que souhaitez-vous modifier ?",
        "booked": "Votre rendez-vous est réservé pour {time}.",
        "cancelled": "Votre rendez-vous a été annulé.",
        "moved": "Votre rendez-vous a été déplacé à {time}.",
        "unavailable": "Je ne peux pas traiter cette demande maintenant. Contactez l'accueil.",
        "knowledge_unavailable": "Je n'ai pas trouvé d'information approuvée à ce sujet.",
    },
    "de": {
        "one_moment": "Einen Moment bitte.",
        "staff_will_reply": "Das konnte ich nicht abschließen. Ich habe es an das Team weitergegeben; es meldet sich in Kürze.",
        "say_again": "Entschuldigung, können Sie das wiederholen?",
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
        "book_offer": "Soll ich Ihren Termin für {time} buchen?",
        "cancel_offer": "Soll ich Ihren Termin für {time} absagen?",
        "move_offer": "Soll ich Ihren Termin von {old} auf {new} verschieben?",
        "confirm": "{provider}. {offer} Sagen Sie Ja zur Bestätigung.",
        "discarded": "Ich habe diesen Vorschlag verworfen. Was möchten Sie ändern?",
        "booked": "Ihr Termin ist für {time} gebucht.",
        "cancelled": "Ihr Termin wurde abgesagt.",
        "moved": "Ihr Termin wurde auf {time} verschoben.",
        "unavailable": (
            "Ich kann diese Anfrage gerade nicht bearbeiten. Bitte wenden Sie sich an den Empfang."
        ),
        "knowledge_unavailable": "Dazu habe ich keine freigegebenen Informationen gefunden.",
    },
    "pt": {
        "one_moment": "Um momento, por favor.",
        "staff_will_reply": "Não consegui concluir. Passei o pedido à equipa, que responderá em breve.",
        "say_again": "Desculpe, pode repetir?",
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
        "book_offer": "Posso agendar seu atendimento para {time}?",
        "cancel_offer": "Posso cancelar seu agendamento de {time}?",
        "move_offer": "Posso mudar seu agendamento de {old} para {new}?",
        "confirm": "{provider}. {offer} Diga sim para confirmar.",
        "discarded": "Descartei essa proposta. O que deseja alterar?",
        "booked": "Seu atendimento está agendado para {time}.",
        "cancelled": "Seu agendamento foi cancelado.",
        "moved": "Seu agendamento foi alterado para {time}.",
        "unavailable": (
            "Não posso concluir essa solicitação agora. Entre em contato com a recepção."
        ),
        "knowledge_unavailable": "Não encontrei informações aprovadas sobre isso.",
    },
    "hi": {
        "one_moment": "एक क्षण रुकिए।",
        "staff_will_reply": "मैं यह पूरा नहीं कर सका। मैंने आपका अनुरोध टीम को भेज दिया है, वे जल्द ही आपसे संपर्क करेंगे।",
        "say_again": "माफ़ कीजिए, क्या आप दोबारा कह सकते हैं?",
        "access_denied": "पहचान की पुष्टि नहीं हो सकी। कृपया रिसेप्शन से संपर्क करें।",
        "identified": "इस कॉल के लिए रिसेप्शन द्वारा अनुमत पहुँच सक्रिय है।",
        "identity_required": "जाँच के बाद कृपया रिसेप्शन से कॉल एक्सेस कोड लें।",
        "slot_unavailable": "यह समय अब उपलब्ध नहीं है। कृपया दूसरा समय चुनें।",
        "appointment_unavailable": ("इस अपॉइंटमेंट को बदला नहीं जा सकता। कृपया रिसेप्शन से संपर्क करें।"),
        "new_slot_unavailable": "नया समय उपलब्ध नहीं है। कृपया दूसरा समय चुनें।",
        "book_offer": "क्या आपकी अपॉइंटमेंट {time} पर बुक कर दूँ?",
        "cancel_offer": "क्या आपकी {time} की अपॉइंटमेंट रद्द कर दूँ?",
        "move_offer": "क्या आपकी अपॉइंटमेंट {old} से {new} पर बदल दूँ?",
        "confirm": "{provider}। {offer} पुष्टि के लिए हाँ कहें।",
        "discarded": "वह प्रस्ताव हटा दिया है। आप क्या बदलना चाहेंगे?",
        "booked": "आपकी अपॉइंटमेंट {time} के लिए बुक हो गई है।",
        "cancelled": "आपकी अपॉइंटमेंट रद्द हो गई है।",
        "moved": "आपकी अपॉइंटमेंट {time} पर बदल दी गई है।",
        "unavailable": "अभी यह अनुरोध पूरा नहीं हो सकता। कृपया रिसेप्शन से संपर्क करें।",
        "knowledge_unavailable": "मुझे इस बारे में स्वीकृत जानकारी नहीं मिली।",
    },
    "hinglish": {
        "one_moment": "Ek minute rukiye.",
        "staff_will_reply": "Main yeh complete nahi kar paya. Maine aapki request team ko bhej di hai, woh jaldi aapse contact karenge.",
        "say_again": "Sorry, kya aap dobara bol sakte hain?",
        "access_denied": "Access confirm nahi ho saka. Kripya reception se baat karein.",
        "identified": "Is call ke liye reception-authorized access active hai.",
        "identity_required": "Verification ke baad reception se call access code lein.",
        "slot_unavailable": "Yeh slot ab available nahi hai. Kripya doosra time chunein.",
        "appointment_unavailable": (
            "Yeh appointment change nahi ho sakti. Reception se baat karein."
        ),
        "new_slot_unavailable": "Naya slot available nahi hai. Kripya doosra time chunein.",
        "book_offer": "Kya aapki appointment {time} par book kar doon?",
        "cancel_offer": "Kya aapki {time} ki appointment cancel kar doon?",
        "move_offer": "Kya appointment {old} se {new} par move kar doon?",
        "confirm": "{provider}. {offer} Confirm karne ke liye haan kahein.",
        "discarded": "Woh proposal hata diya hai. Aap kya change karna chahenge?",
        "booked": "Aapki appointment {time} ke liye book ho gayi hai.",
        "cancelled": "Aapki appointment cancel ho gayi hai.",
        "moved": "Aapki appointment {time} par move ho gayi hai.",
        "unavailable": "Abhi yeh request poori nahi ho sakti. Reception se baat karein.",
        "knowledge_unavailable": "Is baare mein approved information nahi mili.",
    },
}


def spoken(language: str, key: str, **values: Any) -> str:
    templates = MESSAGES.get(language, MESSAGES["en"])
    # A sentence not yet translated is said in English rather than not at all.
    return (templates.get(key) or MESSAGES["en"][key]).format(**values)
