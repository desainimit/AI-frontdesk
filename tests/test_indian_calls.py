"""From the 30 Oct calls: Sarvam's voice and listening, no cutting off, one goodbye, and a
caller told when a clinic does not speak their language. Every service here is a fake."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.ai.engine import Done, LanguageGate, Say
from app.ai.prompts import INDIA, TONES, language_rule
from app.ai.tools.appointments import AppointmentFlow
from app.ai.wording import spoken
from app.conversation.backchannel import (
    _ANYTHING_ELSE as _ANYTHING,  # pyright: ignore[reportPrivateUsage]
)
from app.conversation.backchannel import interrupts, is_line_check, says_farewell, wants_to_end
from app.conversation.confirmation import confirmation_answer
from app.conversation.language import observe_language, reply_fits
from app.conversation.locales import LanguagePolicy
from app.conversation.manager import ConversationStateManager
from app.conversation.promises import announces_lookup, asked_not_chose
from app.conversation.state import LanguageState
from app.integrations.catalog import spec_for, validate_config
from app.integrations.verifiers import HttpProviderVerifier, Verification
from app.languages import CODES, name_in, pack
from app.safety.classifier import SafetySignal
from app.safety.engine import SafetyEngine
from app.voice.opening import Line as Opening
from app.voice.stt.base import TranscriptionResult
from app.voice.stt.sarvam import SarvamSTTProvider
from app.voice.tts.sarvam import SarvamTTSProvider
from tests.test_voice_interruptions import Engine, call, until
from tests.test_voice_latency import ScriptedEngine, receptionist_for, started_pipeline
from tests.voice_sim import Line, TimedTTS

KEY = SecretStr("k" * 20)


def query(provider: SarvamSTTProvider) -> dict[str, list[str]]:
    return parse_qs(urlsplit("x?" + provider._query(8000, "linear16")).query)  # pyright: ignore[reportPrivateUsage]


def test_saaras_is_told_the_context_but_given_no_words_to_imagine() -> None:
    heard = query(SarvamSTTProvider(KEY))
    assert heard["model"] == ["saaras:v4"] and heard["mode"] == ["codemix"]
    assert heard["language_code"] == ["auto"] and "clinic in India" in heard["prompt"][0]
    # On 30 Oct keyterms came back from silence as whole caller lines.
    assert "keyterms" not in heard


def test_english_alone_is_plain_transcription() -> None:
    assert query(SarvamSTTProvider(KEY, "saaras:v3-realtime", "en"))["mode"] == ["transcribe"]


def test_each_language_has_its_own_voice_and_hinglish_is_spoken_by_the_hindi_one() -> None:
    voice = SarvamTTSProvider(KEY, voices={"hi": "a_hi_x", "gu": "b_gu_x", "en": "c_en_x"})
    speaker = voice._speaker  # pyright: ignore[reportPrivateUsage]
    assert speaker("hi") == speaker("hi-Latn") == "a_hi_x"
    assert speaker("gu") == "b_gu_x" and speaker("en") == "c_en_x" and speaker(None) == "c_en_x"


def test_a_change_of_voice_or_pace_does_not_replay_old_phrases() -> None:
    slow = SarvamTTSProvider(KEY, speed="slow").voice_key
    assert slow != SarvamTTSProvider(KEY, speed="normal").voice_key
    assert slow != SarvamTTSProvider(KEY, speed="slow", voices={"hi": "a_hi_x"}).voice_key
    # Bulbul v3 keeps its one voice.
    assert SarvamTTSProvider(KEY, model="bulbul:v3", speaker="ritu").voices == {}


def test_a_voice_setting_must_be_a_bulbul_persona() -> None:
    spec = spec_for("tts", "sarvam")
    config = validate_config(spec, {"speaker_hindi": "ritu_hi_customer_warm"})
    assert config["model"] == "bulbul:v4-flash"
    with pytest.raises(ValueError):
        validate_config(spec, {"speaker_hindi": "Not A Voice"})


def test_a_sarvam_call_holds_hinglish_as_hindi() -> None:
    state = LanguageState(native_script=True)
    state.establish("hi-Latn", 0.9)
    assert state.primary_language == "hi"
    assert LanguageState().primary_language == "en"
    other = LanguageState()
    other.establish("hi-Latn", 0.9)
    assert other.primary_language == "hi-Latn"


def test_gujarati_in_latin_letters_is_not_taken_for_hinglish() -> None:
    reading = observe_language("To mane tame kahish koi koi available che?")
    assert reading is not None and reading.language == "gu"


def caller(text: str, enabled: list[str], native: bool = True) -> LanguageState:
    state = ConversationStateManager.create_initial_state(
        call_id=__import__("uuid").uuid4(),
        tenant_id=__import__("uuid").uuid4(),
        telephony_call_id="CA1",
        language_policy=LanguagePolicy(default_language="en", enabled_languages=enabled),
    )
    state.language_state.native_script = native
    ConversationStateManager.process_caller_utterance(state, text)
    return state.language_state


def test_an_indian_language_the_clinic_has_off_is_noticed() -> None:
    seen = caller("મારે એક અપોઇન્ટમેન્ટ બુક કરવી છે કાલની", ["en", "hi"])
    assert seen.unavailable_heard == "gu" and seen.primary_language == "en"
    assert caller("I need an appointment for tomorrow", ["en", "hi"]).unavailable_heard is None
    on = caller("મારે એક અપોઇન્ટમેન્ટ બુક કરવી છે કાલની", ["en", "gu"])
    assert on.unavailable_heard is None and on.primary_language == "gu"


async def test_the_caller_is_told_once_then_answered(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(ScriptedEngine([(0.0, Say("Tomorrow at nine is free."))]))
    languages = receptionist.state.language_state
    languages.policy = LanguagePolicy(default_language="en", enabled_languages=["en", "hi"])
    languages.note_unavailable("gu")
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("મારે અપોઇન્ટમેન્ટ જોઈએ છે", True))
        languages.note_unavailable("gu")
        receptionist.engine = ScriptedEngine([(0.0, Say("Nine it is."))])  # type: ignore[assignment]
        await receptionist.reply(TranscriptionResult("કાલે સવારે", True))
    finally:
        await pipeline.close()
    said = [turn.text for turn in receptionist.state.transcript_turns if turn.speaker == "agent"]
    offer = said[0]
    assert (
        "Gujarati" in offer
        and "English or Hindi" in offer
        and said[1:]
        == [
            "Tomorrow at nine is free.",
            "Nine it is.",
        ]
    )


@pytest.mark.parametrize(
    ("words", "stops"),
    [
        ("Hmm", False),
        ("Hmmmm", False),
        ("Ha ha", False),
        ("Hey", False),
        ("Extra", False),
        ("ओके।", False),
        ("હમ્મ", False),
        ("wait", True),
        ("I remember the time", True),
        ("नहीं मुझे शाम का time चाहिए", True),
    ],
)
def test_only_real_words_stop_the_receptionist(words: str, stops: bool) -> None:
    assert interrupts(words, "hi") is stops


def test_a_line_check_is_only_that() -> None:
    assert is_line_check("Hello?") and is_line_check("हैलो हैलो") and is_line_check("Hey")
    assert not is_line_check("Hello, I need a doctor")


async def test_a_hmm_does_not_cut_the_receptionist_off() -> None:
    pipeline, _, stt, _ = await call(Engine())
    pipeline.interruption.set_synthesizing(True)
    try:
        for words in ("Hmm", "Ha ha", "Extra"):
            stt.stream.say(words, final=False)
            await asyncio.sleep(0.02)
        assert not pipeline.interruption.is_interrupted
        stt.stream.say("no I wanted the evening", final=False)
        await until(lambda: pipeline.interruption.is_interrupted)
    finally:
        await pipeline.close()


async def test_the_greeting_first_sentence_is_said_whole_and_the_caller_answered_after() -> None:
    pipeline, _, stt, line = await call(Engine())
    turns: list[str] = []
    original = pipeline.on_transcript

    async def heard(result: TranscriptionResult) -> None:
        turns.append(result.text)
        assert original is not None

    pipeline.on_transcript = heard
    pipeline._protected = True  # pyright: ignore[reportPrivateUsage]
    try:
        stt.stream.say("So I did not feel like it", final=False)
        stt.stream.say("So I did not feel like it", final=True)
        await asyncio.sleep(0.03)
        assert not pipeline.interruption.is_interrupted and not turns
        pipeline._greeting_heard()  # pyright: ignore[reportPrivateUsage]
        await until(lambda: bool(turns))
    finally:
        await pipeline.close()
    assert turns == ["So I did not feel like it"] and line is not None


async def test_opening_protects_only_up_to_its_first_recorded_line() -> None:
    pipeline, _, _, _ = await call(Engine())
    states: list[bool] = []
    play = pipeline.play

    async def watched(*args: Any, **kwargs: Any) -> bool:
        states.append(pipeline._protected)  # pyright: ignore[reportPrivateUsage]
        return await play(*args, **kwargs)

    pipeline.play = watched  # type: ignore[method-assign]
    try:
        await pipeline.speak_lines(
            [
                Opening("Hi Nimit.", record=False),
                Opening("Thank you for calling."),
                Opening("Bye."),
            ],
            beat=0,
            protect_first=True,
        )
    finally:
        await pipeline.close()
    assert states == [True, True, False]


async def test_hello_after_a_reply_that_never_played_says_it_again(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    line, tts = Line(), TimedTTS(first_byte=0.0, fail_on="broken")
    receptionist = receptionist_for(
        ScriptedEngine([(0.0, Say("Tomorrow is broken.")), (0.0, Done("answered", ""))])
    )
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("Any time tomorrow?", True))
        assert receptionist._unsaid == ["Tomorrow is broken."]  # pyright: ignore[reportPrivateUsage]
        tts.fail_on = "nothing"
        await receptionist.reply(TranscriptionResult("Hey?", True))
    finally:
        await pipeline.close()
    said = [turn.text for turn in receptionist.state.transcript_turns if turn.speaker == "agent"]
    assert said == ["Tomorrow is broken."]


def test_a_goodbye_is_told_from_a_question() -> None:
    assert says_farewell("Theek hai, koi baat nahi. Apna dhyan rakhiyega.")
    assert says_farewell("ठीक है, अपना ध्यान रखिए।") and says_farewell("તમારો દિવસ શુભ રહે.")
    assert says_farewell("Thank you, take care.")
    assert not says_farewell("Take care of the form. Shall I book it?")


async def test_the_goodbye_is_said_once(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    hung_up = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.hang_up", hung_up)
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(ScriptedEngine([(0.0, Say("Theek hai, apna dhyan rakhiye."))]))
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("nahi abhi kuch nahi karna", True))
        assert receptionist.state.said_farewell and not hung_up.await_count
        await receptionist.reply(TranscriptionResult("ओके।", True))
    finally:
        await pipeline.close()
    said = [turn.text for turn in receptionist.state.transcript_turns if turn.speaker == "agent"]
    assert len(said) == 1 and hung_up.await_count == 1


async def test_silence_after_a_goodbye_ends_the_call(monkeypatch: Any) -> None:
    hung_up = AsyncMock()
    monkeypatch.setattr("app.voice.receptionist.hang_up", hung_up)
    pipeline, receptionist, _, _ = await call(Engine())
    receptionist.state.said_farewell = True
    try:
        await receptionist.silence(1)
    finally:
        await pipeline.close()
    assert hung_up.await_count == 1
    assert not [turn for turn in receptionist.state.transcript_turns if turn.speaker == "agent"]


def test_a_lookup_announcement_is_not_a_reply() -> None:
    assert announces_lookup("Let me check karte hain ki kal ke liye kya time available hai.")
    assert announces_lookup("जी, मैं देखती हूँ।")
    assert not announces_lookup("Kal shaam 5 baje khaali hai.")
    assert not announces_lookup("Kya main dekh loon?")


async def test_the_server_says_the_lookup_line_not_the_model(monkeypatch: Any) -> None:
    monkeypatch.setattr("app.voice.receptionist.record_turn", AsyncMock())
    line, tts = Line(), TimedTTS(first_byte=0.0)
    receptionist = receptionist_for(
        ScriptedEngine([(0.0, Say("Let me check that.")), (0.0, Say("Nine is free."))])
    )
    pipeline = await started_pipeline(line, tts)
    receptionist.pipeline = pipeline
    try:
        await receptionist.reply(TranscriptionResult("Any time tomorrow?", True))
    finally:
        await pipeline.close()
    said = [turn.text for turn in receptionist.state.transcript_turns if turn.speaker == "agent"]
    assert said == ["Nine is free."]


def test_the_voice_rules_ask_for_calm_and_courtesy() -> None:
    assert "upbeat" not in TONES["friendly"] and "calm" in TONES["friendly"]
    assert "आप" in INDIA and "Let me check" in INDIA
    assert "Latin letters" in language_rule("hi") and "Latin letters" in language_rule("gu")


@pytest.mark.parametrize("code", CODES)
def test_every_language_can_say_it_does_not_speak_one(code: str) -> None:
    line = spoken(code, "language_offer", heard=name_in("gu", code), offer=name_in("en", code))
    assert name_in("gu", code) in line and pack(code).messages["word_or"]


@pytest.mark.parametrize("code", ["hi", "hi-Latn", "gu"])
def test_fixed_lines_do_not_give_the_receptionist_a_mans_verbs(code: str) -> None:
    item = pack(code)
    lines = " ".join([*item.messages.values(), *item.safety.values(), *item.handover.values()])
    for word in ("कराता हूँ", "नहीं कर सका", "दे सकता", "karta hoon", "kar paya", "શક્યો નથી. તમારી"):
        assert word not in lines


async def test_a_restricted_elevenlabs_key_is_named_as_that() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": {"status": "missing_permissions"}})

    verifier = HttpProviderVerifier(transport=httpx.MockTransport(refuse))
    result = await verifier.verify(
        "tts", "elevenlabs", None, {"voice_id": "v"}, {"api_key": "k" * 20}
    )
    assert result == Verification("invalid", "missing_permissions")


async def test_a_key_that_can_speak_passes_without_listing_voices() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(401, json={"detail": {"status": "missing_permissions"}})
        return httpx.Response(200, content=b"ID3")

    verifier = HttpProviderVerifier(transport=httpx.MockTransport(answer))
    result = await verifier.verify(
        "tts", "elevenlabs", None, {"voice_id": "v", "model_id": "m"}, {"api_key": "k" * 20}
    )
    assert result.outcome == "verified"


class Alarmed:
    """A second opinion that is worried by everything it is asked about."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def classify(self, utterance: str, context_hint: str) -> SafetySignal:
        self.asked.append(utterance)
        return SafetySignal(kind="uncertain", confidence=0.9, reason_code="unclear")


def safety_state() -> Any:
    return ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA1")


@pytest.mark.parametrize("words", ["Yes yes please.", "No, thanks.", "हाँ जी"])
async def test_a_bare_yes_or_no_does_not_raise_a_health_question(words: str) -> None:
    classifier = Alarmed()
    engine = SafetyEngine(classifier=classifier)  # pyright: ignore[reportArgumentType]
    assert await engine.evaluate(safety_state(), words) is None
    assert classifier.asked == []


async def test_anything_more_than_a_yes_still_gets_the_second_opinion() -> None:
    classifier = Alarmed()
    engine = SafetyEngine(classifier=classifier)  # pyright: ignore[reportArgumentType]
    assert await engine.evaluate(safety_state(), "Yes, my head feels strange") is not None
    assert classifier.asked == ["Yes, my head feels strange"]


@pytest.mark.parametrize(
    ("words", "refused"),
    [
        ("Not not. Can you do have a follow-up visit tomorrow?", True),
        ("क्या कल follow-up हो सकता है?", True),
        ("Can I come at 9?", False),
        ("Yes, that one.", False),
        ("Is tomorrow at 9 a.m. free?", False),
        ("Is tomorrow morning at nine free?", False),
    ],
)
def test_a_question_is_not_a_choice_of_time(words: str, refused: bool) -> None:
    assert asked_not_chose(words, [{"when": "morning at nine"}]) is refused


@pytest.mark.parametrize(
    "words",
    ["હા કરી દયો.", "હા કરી દયો ને.", "હા કરી દો.", "हाँ कर दीजिए", "haan kar do", "हाँ रख दो"],
)
def test_a_yes_said_the_gujarati_or_hindi_way_books_it(words: str) -> None:
    assert confirmation_answer(words) is True


@pytest.mark.parametrize("words", ["નહીં નહીં રાખના", "ના રહેવા દો", "નહીં ત્રણ વાગ્યાનો રાખો"])
def test_a_no_stays_a_no(words: str) -> None:
    assert confirmation_answer(words) is False


@pytest.mark.parametrize(
    ("text", "language", "fits"),
    [
        ("कृपया बताइए आपकी क्या मदद कर सकती हूँ", "gu", False),
        ("કૃપા করে appointment પર જે નામ હોય તે જણાવશો?", "gu", False),
        ("જી, કાલે સવારે નવ વાગ્યે ખાલી છે.", "gu", True),
        ("जी, कल सुबह नौ बजे का time खाली है।", "hi", True),
        ("जी, કાલે सुबह नौ बजे", "hi", False),
    ],
)
def test_a_reply_keeps_to_the_call_language(text: str, language: str, fits: bool) -> None:
    assert reply_fits(text, language) is fits


def test_a_later_sentence_in_another_language_is_left_out() -> None:
    gate = LanguageGate("gu", check=True)
    said = gate.feed("હું સનરાઈઝ ક્લિનિકમાંથી આશા બોલું છું.")
    said += gate.feed("बताइए, आपकी क्या मदद कर सकती हूँ?")
    assert said == ["હું સનરાઈઝ ક્લિનિકમાંથી આશા બોલું છું."] and not gate.wrong


def doctors_session(*names: str) -> Any:
    directory = {"doctors": [{"name": name, "specialty": "general"} for name in names]}
    return MagicMock(directory=directory)


def test_the_caller_chooses_the_doctor_and_is_asked_once() -> None:
    state = safety_state()
    session = doctors_session("Dr. Asha", "Dr. Dhruvil")
    asked = AppointmentFlow._ask_doctor_first(session, state)  # pyright: ignore[reportPrivateUsage]
    assert asked is not None and asked.status == "ask_which_doctor" and len(asked.items) == 2
    assert AppointmentFlow._ask_doctor_first(session, state) is None  # pyright: ignore[reportPrivateUsage]


def test_a_doctor_the_caller_named_or_the_only_one_needs_no_question() -> None:
    state = safety_state()
    state.append_transcript_turn("caller", "ડૉક્ટર આશા સાથે appointment જોઈએ છે")
    session = doctors_session("Dr. Asha", "Dr. Dhruvil")
    assert AppointmentFlow._ask_doctor_first(session, state) is None  # pyright: ignore[reportPrivateUsage]
    alone = doctors_session("Dr. Asha")
    assert AppointmentFlow._ask_doctor_first(alone, safety_state()) is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("code", CODES)
def test_after_a_booking_the_caller_is_asked_if_there_is_anything_else(code: str) -> None:
    line = spoken(code, "booked", time="x", reference="A-1")
    assert _ANYTHING.search(line)


def test_a_fragment_after_anything_else_is_not_a_goodbye() -> None:
    last = "Anything else I can help with?"
    assert not wants_to_end("So,", last) and not wants_to_end("I want to reschedule it.", last)
    assert wants_to_end("No, thanks.", last) and wants_to_end("ના બસ, આભાર.", "બીજું કંઈ?")


def speaking(*enabled: str) -> Any:
    state = safety_state()
    state.language_state.policy = LanguagePolicy(
        default_language="en", enabled_languages=list(enabled)
    )
    state.language_state.native_script = True
    return state


def say(state: Any, text: str) -> str:
    ConversationStateManager.process_caller_utterance(state, text)
    return state.language


def test_a_clear_hindi_or_gujarati_sentence_moves_the_call_at_once() -> None:
    state = speaking("en", "hi", "gu")
    say(state, "I want to know about my appointment")
    assert say(state, "No, I ના ના એમ નહીં નહીં મને સાંજની કહી શકશો") == "gu"
    assert say(state, "तो मुझे गुरुवार का 9 बजे का time देखना है") == "hi"


def test_a_hindi_sentence_full_of_english_words_is_still_hindi() -> None:
    reading = observe_language("मुझे Tuesday का time appointment available चाहिए")
    assert reading is not None and reading.language == "hi"


def test_a_few_english_words_do_not_move_a_gujarati_call() -> None:
    state = speaking("en", "gu")
    say(state, "મારે કાલની appointment જોવી છે કોઈ available હશે?")
    assert say(state, "So I did not feel like it") == "gu"
    assert say(state, "Can you give me the list of Tuesday times") == "en"


def test_a_caller_mixing_three_languages_is_followed_to_the_latest() -> None:
    state = speaking("en", "hi", "gu")
    say(state, "મારે કાલની appointment જોવી છે કોઈ available હશે?")
    say(state, "Can you give me the list of Tuesday times")
    assert say(state, "मुझे सोमवार का time बता दीजिए") == "hi"


DOCTORS = (
    {"id": "d1", "name": "Dr. Asha", "specialty": "general"},
    {"id": "d2", "name": "Dr. Dhruvil", "specialty": "general"},
)


def clinic_with_two_doctors() -> Any:
    return MagicMock(directory={"doctors": list(DOCTORS)})


def test_the_doctor_they_chose_stays() -> None:
    state, session = safety_state(), clinic_with_two_doctors()
    state.append_transcript_turn("caller", "Dhruvil સાથે appointment જોઈએ છે")
    ask = AppointmentFlow._ask_doctor_first  # pyright: ignore[reportPrivateUsage]
    keep = AppointmentFlow._keep_their_doctor  # pyright: ignore[reportPrivateUsage]
    assert ask(session, state) is None and state.chosen_doctor == "d2"
    other = keep(session, state, "d1")
    assert other is not None and other.status == "they_chose_another_doctor"
    assert other.items[0]["doctor"] == "Dr. Dhruvil"
    assert keep(session, state, "d2") is None


def test_no_doctor_chosen_is_no_reason_to_refuse_one() -> None:
    state, session = safety_state(), clinic_with_two_doctors()
    keep = AppointmentFlow._keep_their_doctor  # pyright: ignore[reportPrivateUsage]
    assert keep(session, state, "d1") is None
