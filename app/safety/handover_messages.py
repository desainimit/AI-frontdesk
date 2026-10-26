"""What the receptionist says around a transfer: it always asks before putting anyone
through, and says plainly what happens next."""

# ruff: noqa: E501

from typing import Any

# Hindi and Gujarati first-person verbs are gendered; these lines avoid them.
HANDOVER: dict[str, dict[str, str]] = {
    "en": {
        "offer_first": "Sure, I can connect you. Is it something I can help with, like booking or a question, or shall I put you through now?",
        "offer": "I can put you through to {target}. Shall I do that now?",
        "connecting": "Sure, let me get you over to someone on the team.",
        "declined": "No problem. How can I help?",
        "offer_message": "I can't put you through right now, but I can take a message so the team calls you back. Would you like that?",
        "message_taken": "Done. I've passed it on, and someone from the team will call you back.",
        "trouble_offer": "Sorry, I'm having trouble on my side. Shall I put you through to the team?",
        "trouble_message": "Sorry, I'm having trouble on my side. Shall I take a message so the team calls you back?",
        "after_emergency": "I can also connect you to the clinic. Would you like that?",
        "no_answer": "Sorry, the team couldn't pick up just now. I've noted it down and someone will call you back. Anything else I can help with?",
    },
    "es": {
        "offer_first": "Claro, puedo pasarle. ¿Es algo en lo que pueda ayudarle, como una cita o una pregunta, o le paso ahora?",
        "offer": "Puedo pasarle con {target}. ¿Lo hago ahora?",
        "connecting": "Por supuesto, le paso ahora. Un momento.",
        "declined": "Sin problema. ¿En qué le ayudo?",
        "offer_message": "Ahora no puedo pasarle, pero puedo dejar un mensaje para que el equipo le llame. ¿Le parece?",
        "message_taken": "Hecho. Lo he pasado al equipo y le llamarán.",
        "trouble_offer": "Perdone, tengo un problema técnico. ¿Le paso con el equipo?",
        "trouble_message": "Perdone, tengo un problema técnico. ¿Dejo un mensaje para que el equipo le llame?",
        "after_emergency": "También puedo pasarle con la clínica. ¿Quiere?",
        "no_answer": "Lo siento, el equipo no ha podido atender ahora. He anotado su petición y le llamarán. ¿Algo más en lo que pueda ayudarle?",
    },
    "fr": {
        "offer_first": "Bien sûr, je peux vous mettre en relation. Est-ce quelque chose que je peux régler, comme un rendez-vous ou une question, ou je vous passe quelqu'un maintenant ?",
        "offer": "Je peux vous passer {target}. Je le fais maintenant ?",
        "connecting": "Bien sûr, je vous mets en relation. Un instant.",
        "declined": "Pas de souci. Comment puis-je vous aider ?",
        "offer_message": "Je ne peux pas vous passer quelqu'un pour le moment, mais je peux laisser un message pour qu'on vous rappelle. Ça vous va ?",
        "message_taken": "C'est fait. J'ai transmis votre demande, quelqu'un de l'équipe vous rappellera.",
        "trouble_offer": "Désolé, j'ai un souci technique. Je vous passe l'équipe ?",
        "trouble_message": "Désolé, j'ai un souci technique. Je laisse un message pour qu'on vous rappelle ?",
        "after_emergency": "Je peux aussi vous mettre en relation avec la clinique. Vous voulez ?",
        "no_answer": "Désolé, l'équipe n'a pas pu répondre. J'ai noté votre demande, quelqu'un vous rappellera. Puis-je vous aider pour autre chose ?",
    },
    "de": {
        "offer_first": "Gern verbinde ich Sie. Kann ich vielleicht selbst helfen, etwa mit einem Termin oder einer Frage, oder soll ich Sie gleich durchstellen?",
        "offer": "Ich kann Sie mit {target} verbinden. Soll ich das jetzt tun?",
        "connecting": "Natürlich, ich verbinde Sie jetzt. Einen Moment.",
        "declined": "Kein Problem. Wie kann ich helfen?",
        "offer_message": "Ich kann Sie gerade nicht durchstellen, aber eine Nachricht hinterlassen, damit das Team zurückruft. Ist das recht?",
        "message_taken": "Erledigt. Ich habe es weitergegeben, das Team ruft Sie zurück.",
        "trouble_offer": "Entschuldigung, ich habe gerade technische Probleme. Soll ich Sie mit dem Team verbinden?",
        "trouble_message": "Entschuldigung, ich habe gerade technische Probleme. Soll ich eine Nachricht für einen Rückruf hinterlassen?",
        "after_emergency": "Ich kann Sie auch mit der Praxis verbinden. Möchten Sie das?",
        "no_answer": "Leider konnte gerade niemand abnehmen. Ich habe Ihr Anliegen notiert, man ruft Sie zurück. Kann ich sonst noch helfen?",
    },
    "pt": {
        "offer_first": "Claro, posso transferir. É algo em que eu possa ajudar, como uma consulta ou uma dúvida, ou prefere que eu transfira agora?",
        "offer": "Posso transferir para {target}. Faço isso agora?",
        "connecting": "Claro, transferindo agora. Um momento.",
        "declined": "Sem problema. Como posso ajudar?",
        "offer_message": "Agora não consigo transferir, mas posso deixar um recado para a equipe ligar de volta. Pode ser?",
        "message_taken": "Pronto. Passei o recado e alguém da equipe vai ligar de volta.",
        "trouble_offer": "Desculpe, estou com um problema técnico. Quer que eu transfira para a equipe?",
        "trouble_message": "Desculpe, estou com um problema técnico. Quer que eu deixe um recado para a equipe ligar?",
        "after_emergency": "Também posso transferir para a clínica. Quer?",
        "no_answer": "Desculpe, a equipe não conseguiu atender agora. Anotei seu pedido e vão ligar de volta. Posso ajudar em mais alguma coisa?",
    },
    "hi": {
        "offer_first": "जी, बात करवा सकते हैं। क्या यह कुछ ऐसा है जिसमें यहीं मदद हो सके, जैसे अपॉइंटमेंट या कोई सवाल, या अभी कनेक्ट कर दें?",
        "offer": "आपको {target} से जोड़ सकते हैं। अभी जोड़ दें?",
        "connecting": "जी, अभी कनेक्ट कर रहे हैं। एक पल।",
        "declined": "कोई बात नहीं। बताइए, क्या मदद करें?",
        "offer_message": "अभी कनेक्ट नहीं हो पाएगा, पर टीम के लिए संदेश छोड़ सकते हैं ताकि वे वापस कॉल करें। ठीक है?",
        "message_taken": "हो गया। संदेश टीम तक पहुँचा दिया है, वे आपको वापस कॉल करेंगे।",
        "trouble_offer": "माफ़ कीजिए, अभी तकनीकी दिक्कत है। क्या टीम से जोड़ दें?",
        "trouble_message": "माफ़ कीजिए, अभी तकनीकी दिक्कत है। क्या टीम के लिए संदेश छोड़ दें ताकि वे कॉल करें?",
        "after_emergency": "क्लिनिक से भी जोड़ सकते हैं। जोड़ दें?",
        "no_answer": "माफ़ कीजिए, टीम अभी कॉल नहीं उठा पाई। आपका अनुरोध नोट कर लिया है, वे वापस कॉल करेंगे। और कुछ मदद चाहिए?",
    },
    "hinglish": {
        "offer_first": "Ji, baat karwa sakte hain. Kya yeh kuch aisa hai jismein yahin madad ho sake, jaise appointment ya koi sawaal, ya abhi connect kar dein?",
        "offer": "Aapko {target} se jod sakte hain. Abhi jod dein?",
        "connecting": "Ji, abhi connect kar rahe hain. Ek pal.",
        "declined": "Koi baat nahi. Bataiye, kya madad karein?",
        "offer_message": "Abhi connect nahi ho payega, par team ke liye message chhod sakte hain taaki woh call back karein. Theek hai?",
        "message_taken": "Ho gaya. Message team tak pahuncha diya hai, woh aapko call back karenge.",
        "trouble_offer": "Sorry, abhi technical dikkat hai. Kya team se jod dein?",
        "trouble_message": "Sorry, abhi technical dikkat hai. Kya team ke liye message chhod dein?",
        "after_emergency": "Clinic se bhi jod sakte hain. Jod dein?",
        "no_answer": "Sorry, team abhi call nahi utha payi. Aapki request note kar li hai, woh call back karenge. Aur kuch madad chahiye?",
    },
    "gu": {
        "offer_first": "જી, વાત કરાવી શકાય. શું આ એવું કંઈ છે જેમાં અહીં જ મદદ થઈ શકે, જેમ કે એપોઇન્ટમેન્ટ કે કોઈ પ્રશ્ન, કે હમણાં જ જોડી દઉં?",
        "offer": "તમને {target} સાથે જોડી શકાય. હમણાં જોડી દઉં?",
        "connecting": "જી, હમણાં જોડી રહ્યાં છીએ. એક ક્ષણ.",
        "declined": "કોઈ વાંધો નહીં. કહો, શું મદદ કરું?",
        "offer_message": "અત્યારે જોડી શકાય તેમ નથી, પણ ટીમ માટે સંદેશ મૂકી શકાય જેથી તેઓ પાછો કૉલ કરે. ચાલશે?",
        "message_taken": "થઈ ગયું. સંદેશ ટીમને પહોંચાડી દીધો છે, તેઓ તમને પાછો કૉલ કરશે.",
        "trouble_offer": "માફ કરશો, અત્યારે તકનીકી તકલીફ છે. ટીમ સાથે જોડી દઉં?",
        "trouble_message": "માફ કરશો, અત્યારે તકનીકી તકલીફ છે. ટીમ માટે સંદેશ મૂકી દઉં?",
        "after_emergency": "ક્લિનિક સાથે પણ જોડી શકાય. જોડી દઉં?",
        "no_answer": "માફ કરશો, ટીમ અત્યારે કૉલ ઉપાડી શકી નહીં. તમારી વિનંતી નોંધી લીધી છે, તેઓ પાછો કૉલ કરશે. બીજી કોઈ મદદ જોઈએ?",
    },
}


def handover_message(language: str, key: str, **values: Any) -> str:
    templates = HANDOVER.get(language, HANDOVER["en"])
    return (templates.get(key) or HANDOVER["en"][key]).format(**values)
