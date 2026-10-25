import json
from collections.abc import AsyncGenerator
from string import Formatter
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.ai.wording import MESSAGES, spoken
from app.conversation.confirmation import confirmation_answer
from app.conversation.language import observe_language
from app.conversation.locales import LOCALES, LanguagePolicy
from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConfirmationStatus
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import TranscriptionResult
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.stt.live import DeepgramLiveStream
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider
from tests.confirmations import ask_to_confirm
from tests.test_multilingual_phase10 import SpeechSocketFake

SENTENCES = [
    ("en", "I would like to book an appointment for tomorrow morning"),
    ("es", "Me gustaría reservar una cita para mañana por la mañana"),
    ("fr", "Je voudrais prendre un rendez-vous pour demain matin"),
    ("de", "Ich möchte einen Termin für morgen Vormittag vereinbaren"),
    ("pt", "Gostaria de agendar um atendimento para amanhã de manhã"),
    ("hi", "मुझे कल सुबह के लिए अपॉइंटमेंट बुक करना है"),
]
CONSENT = [
    ("en", "Yes!", "No!", "yes but tomorrow"),
    ("es", "¡Sí!", "No", "sí pero mañana"),
    ("fr", "Oui.", "Non", "oui mais demain"),
    ("de", "Ja!", "Nein", "ja aber morgen"),
    ("pt", "Sim!", "Não", "sim mas amanhã"),
    ("hi", "हाँ।", "नहीं", "हाँ लेकिन कल"),
]


@pytest.mark.parametrize("code,sentence", SENTENCES)
def test_lingua_launch_languages(code: str, sentence: str) -> None:
    observation = observe_language(sentence)
    assert observation is not None and observation.language == code


def test_provider_evidence_policy_and_short_replies() -> None:
    english = observe_language("The main entrance is on the left")
    assert english is not None and english.language == "en"
    mixed = observe_language("Mujhe kal appointment chahiye", provider_languages=("hi", "en"))
    assert mixed is not None and mixed.language == "hinglish" and mixed.code_switching
    assert observe_language("Dr Shah", "en") is None
    assert observe_language("12345678", "en") is None
    assert observe_language("yes yes yes", "en") is None
    assert observe_language("A sufficiently long transcript", "ja") is None
    assert observe_language(SENTENCES[1][1], "es", enabled_languages=("en",)) is None
    assert observe_language(SENTENCES[1][1], enabled_languages=("en",)) is None


def test_tenant_language_stability_and_fixed_policy() -> None:
    policy = LanguagePolicy(default_language="es", enabled_languages=["es", "en"])
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAlanguages", language_policy=policy
    )
    ConversationStateManager.process_caller_utterance(
        state, SENTENCES[1][1], detected_language="es"
    )
    for reply in ["yes", "no", "Dr Shah"]:
        ConversationStateManager.process_caller_utterance(state, reply, detected_language="en")
    assert state.language == "es"
    ConversationStateManager.process_caller_utterance(
        state, SENTENCES[0][1], detected_language="en"
    )
    assert state.language == "es"
    ConversationStateManager.process_caller_utterance(
        state, SENTENCES[0][1], detected_language="en"
    )
    assert state.language == "en"
    state.language_state.policy.auto_detect = False
    for _ in range(3):
        ConversationStateManager.process_caller_utterance(
            state, SENTENCES[1][1], detected_language="es"
        )
    assert state.language == "en"


@pytest.mark.parametrize(
    "payload",
    [
        {"enabled_languages": []},
        {"enabled_languages": ["en", "en"]},
        {"default_language": "de", "enabled_languages": ["en"]},
        {"enabled_languages": ["en", "ja"]},
        {"voice": "unexpected"},
    ],
)
def test_language_policy_rejects_invalid_settings(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        LanguagePolicy.model_validate(payload)


@pytest.mark.parametrize("code,yes,no,revised", CONSENT)
def test_localized_consent_is_exact(code: str, yes: str, no: str, revised: str) -> None:
    assert confirmation_answer(yes, code) is True
    assert confirmation_answer(no, code) is False
    assert confirmation_answer(revised, code) is None
    assert confirmation_answer(yes + " " + no, code) is False
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAlanguages", default_language=code
    )
    ask_to_confirm(state, {"time": "10:00"}, "Confirm?")
    ConversationStateManager.process_caller_utterance(state, revised, detected_language=code)
    assert state.confirmation_state.status != ConfirmationStatus.CONFIRMED
    ConversationStateManager.process_caller_utterance(state, yes)
    assert state.confirmation_state.status != ConfirmationStatus.CONFIRMED
    ask_to_confirm(state, {"time": "11:00"}, "Confirm?")
    ConversationStateManager.process_caller_utterance(state, yes)
    assert state.confirmation_state.status == ConfirmationStatus.CONFIRMED


def test_translation_keys_and_placeholders_match() -> None:
    def placeholders(value: str) -> set[str]:
        return {name for _, name, _, _ in Formatter().parse(value) if name}

    for code in (*LOCALES, "hinglish"):
        # A sentence may wait for its translation; it is then said in English.
        assert MESSAGES[code].keys() <= MESSAGES["en"].keys()
        for key, template in MESSAGES[code].items():
            assert placeholders(template) == placeholders(MESSAGES["en"][key])
        booked = spoken(code, "booked", time="10:00", reference="A-7")
        assert "10:00" in booked and "A-7" in booked


@pytest.mark.parametrize("boundary", ["speech_final", "UtteranceEnd"])
async def test_stream_accumulates_word_language_evidence(boundary: str) -> None:
    def event(text: str, labels: list[str], final: bool = False) -> str:
        return json.dumps(
            {
                "type": "Results",
                "channel": {
                    "alternatives": [
                        {
                            "transcript": text,
                            "confidence": 0.99,
                            "languages": ["en"],  # Word evidence takes precedence.
                            "words": [{"language": label} for label in labels],
                        }
                    ]
                },
                "is_final": True,
                "speech_final": final,
            }
        )

    messages = [
        event("Quiero reservar una cita para mañana", ["es"] * 6),
        event("please", ["en"], boundary == "speech_final"),
    ]
    if boundary == "UtteranceEnd":
        messages.append(json.dumps({"type": "UtteranceEnd"}))
    messages.append(event("I would like another appointment", ["en"] * 6, True))
    stream = DeepgramLiveStream(SpeechSocketFake(messages), "multi", 1)
    results = cast(AsyncGenerator[TranscriptionResult, None], stream.results())
    try:
        result = await anext(results)
        while not result.is_final:
            result = await anext(results)
        assert result.languages == ("es", "en") and result.language == "es"
        assert result.text.endswith("please")
        next_turn = await anext(results)
        assert next_turn.languages == ("en",)
    finally:
        await results.aclose()
        await stream.close()


async def test_batch_uses_alternative_languages() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": {
                    "channels": [
                        {
                            "alternatives": [
                                {
                                    "transcript": SENTENCES[1][1],
                                    "confidence": 0.98,
                                    "languages": ["es", "en"],
                                }
                            ]
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = DeepgramSTTProvider(SecretStr("key"), client=client, language="multi")
        result = await provider.transcribe(b"\x00\x00")
    assert result.language == "es" and result.languages == ("es", "en")


@pytest.mark.parametrize("code,sentence", SENTENCES)
async def test_six_language_speech_requests(code: str, sentence: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\x00\x00")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        eleven = ElevenLabsTTSProvider(SecretStr("key"), client=client)
        azure = AzureTTSProvider(SecretStr("key"), client=client)
        assert [chunk async for chunk in eleven.synthesize(sentence, language=code)]
        assert [chunk async for chunk in azure.synthesize(sentence, language=code)]
    assert json.loads(requests[0].content)["language_code"] == code
    assert LOCALES[code].azure_voice in requests[1].content.decode()


async def test_azure_stt_language_is_dynamic_and_call_local() -> None:
    queries: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["language"])
        return httpx.Response(
            200,
            json={
                "RecognitionStatus": "Success",
                "NBest": [{"Display": SENTENCES[1][1], "Confidence": 0.99}],
            },
        )

    locale_a = "es-ES"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        a = AzureSTTProvider(SecretStr("key"), client=client, language_resolver=lambda: locale_a)
        b = AzureSTTProvider(SecretStr("key"), client=client, language_resolver=lambda: "fr-FR")
        assert (await a.transcribe(b"\x00\x00")).language is None
        await b.transcribe(b"\x00\x00")
        locale_a = "pt-BR"
        await a.transcribe(b"\x00\x00")
        await b.transcribe(b"\x00\x00")
    assert queries == ["es-ES", "fr-FR", "pt-BR", "fr-FR"]
