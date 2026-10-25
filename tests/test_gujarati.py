"""Gujarati policy, transactional consent, safety, and speech transport regressions.

Provider HTTP is mocked. These checks do not claim live pronunciation or ASR quality.
"""

import json
from datetime import UTC, datetime
from string import Formatter
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from app.ai.prompts import system_prompt
from app.ai.session import Session
from app.ai.tools.appointments import AppointmentFlow, Outcome
from app.ai.wording import MESSAGES, spoken
from app.calls.access import IdentifyPatient, normalize_access_code
from app.conversation.confirmation import confirmation_answer
from app.conversation.language import observe_language
from app.conversation.locales import LanguagePolicy, normalize_language
from app.conversation.manager import ConversationStateManager
from app.conversation.redaction import redact_transcript
from app.conversation.state import ConfirmationStatus, ConversationState, SafetyDecision
from app.core.settings import Settings
from app.integrations.catalog import DeepgramConfig, ElevenLabsConfig
from app.integrations.resolver import ResolvedIntegration
from app.notifications.templates import when
from app.safety.classifier import SafetySignal
from app.safety.engine import SafetyEngine
from app.safety.messages import MESSAGES as SAFETY_MESSAGES
from app.safety.normalization import normalize_transcript
from app.safety.rules import deterministic_rule
from app.settings.defaults import default_settings
from app.voice.factory import (
    GujaratiVoiceConfigurationError,
    platform_stt,
    recognition_locales,
    stt_from_connection,
    validate_gujarati_voice,
)
from app.voice.pipeline import VoicePipeline
from app.voice.stt.azure import AzureSTTProvider
from app.voice.stt.base import STTTransientError
from app.voice.stt.deepgram import DeepgramSTTProvider
from app.voice.tts.azure import AzureTTSProvider
from app.voice.tts.base import TTSPermanentError
from app.voice.tts.elevenlabs import ElevenLabsTTSProvider
from tests.confirmations import ask_to_confirm
from tests.fake_stt import MockSTTProvider
from tests.fake_tts import MockTTSProvider

GUJARATI = "મને આવતીકાલે સવારે ડૉક્ટરની મુલાકાત બુક કરાવવી છે"


def gujarati_policy() -> LanguagePolicy:
    return LanguagePolicy(default_language="gu", enabled_languages=["gu"], auto_detect=False)


def multilingual_policy() -> LanguagePolicy:
    return LanguagePolicy(
        default_language="en",
        enabled_languages=["en", "es", "fr", "de", "pt", "hi", "gu"],
        auto_detect=True,
    )


def test_gujarati_switches_without_becoming_the_main_language() -> None:
    policy = multilingual_policy()
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAmultilingual", language_policy=policy
    )
    assert state.language == "en"
    ConversationStateManager.process_caller_utterance(state, GUJARATI, detected_language="gu")
    assert state.language == "gu"
    ConversationStateManager.process_caller_utterance(state, "yes", detected_language="en")
    assert state.language == "gu"
    for _ in range(2):
        ConversationStateManager.process_caller_utterance(
            state, "Please speak in English now", detected_language="en"
        )
    assert state.language == "en"
    assert policy.default_language == "en" and policy.auto_detect


def test_automatic_gujarati_provider_capability_depends_on_providers_not_main_language(
    settings: Settings,
) -> None:
    config = settings.model_copy(update={"stt_provider": "azure", "tts_provider": "azure"})
    policy = multilingual_policy()
    validate_gujarati_voice(config, policy, None, None)
    eleven = config.model_copy(
        update={"tts_provider": "elevenlabs", "elevenlabs_model_id": "eleven_v3"}
    )
    validate_gujarati_voice(eleven, policy, None, None)
    assert ElevenLabsConfig(model_id="eleven_v3").model_id == "eleven_v3"
    with pytest.raises(GujaratiVoiceConfigurationError):
        validate_gujarati_voice(
            config.model_copy(update={"stt_provider": "deepgram", "deepgram_language": "multi"}),
            policy,
            None,
            None,
        )
    # An optional disabled language must not block fixed English calls.
    validate_gujarati_voice(settings, policy.model_copy(update={"auto_detect": False}), None, None)


def test_recognition_candidate_lists_are_call_local(settings: Settings) -> None:
    policy = multilingual_policy()
    config = settings.model_copy(
        update={"stt_provider": "azure", "azure_speech_key": SecretStr("test")}
    )
    provider = platform_stt(config, lambda: "en-US", policy)
    assert isinstance(provider, AzureSTTProvider)
    assert provider.candidate_languages == recognition_locales(policy)
    assert "gu-IN" in provider.candidate_languages
    connection = ResolvedIntegration(
        uuid4(),
        uuid4(),
        uuid4(),
        "azure_speech",
        "stt",
        "primary",
        None,
        {"region": "eastus", "language": "en-US"},
        {"speech_key": SecretStr("test")},
    )
    other = stt_from_connection(
        config,
        connection,
        lambda: "fr-FR",
        LanguagePolicy(default_language="fr", enabled_languages=["fr", "en"]),
    )
    assert isinstance(other, AzureSTTProvider)
    assert other.candidate_languages == ("fr-FR", "en-US")
    assert "gu-IN" in provider.candidate_languages
    assert recognition_locales(gujarati_policy()) == ()


async def test_azure_detects_gujarati_from_audio_with_english_main_language() -> None:
    captured: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        locale, text = (
            ("gu-IN", GUJARATI) if len(captured) == 1 else ("en-US", "Please book tomorrow")
        )
        return httpx.Response(
            200,
            json={
                "combinedPhrases": [{"text": text}],
                "phrases": [{"text": text, "locale": locale, "confidence": 0.97}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AzureSTTProvider(
            SecretStr("test"),
            client=client,
            language_resolver=lambda: "en-US",
            candidate_languages=recognition_locales(multilingual_policy()),
        )
        first = await provider.transcribe(b"\x00\x00")
        second = await provider.transcribe(b"\x00\x00")
    assert first.language == "gu" and first.text == GUJARATI
    assert second.language == "en" and second.languages == ("en",)
    for request in captured:
        assert request.url.path == "/speechtotext/transcriptions:transcribe"
        assert request.url.params["api-version"] == "2025-10-15"
        assert "language" not in request.url.params
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert (
            b'"locales": ["en-US", "es-ES", "fr-FR", "de-DE", "pt-BR", "hi-IN", "gu-IN"]'
            in request.content
        )
        assert b"RIFF" in request.content


@pytest.mark.parametrize("phrases", [[], [{"text": "Untrusted", "locale": "ja-JP"}]])
async def test_silence_or_unexpected_audio_locale_cannot_invent_a_language(
    phrases: list[dict[str, str]],
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"phrases": phrases}))
    ) as client:
        provider = AzureSTTProvider(
            SecretStr("test"), client=client, candidate_languages=("en-US", "gu-IN")
        )
        if phrases:
            with pytest.raises(STTTransientError):
                await provider.transcribe(b"\x00\x00")
        else:
            result = await provider.transcribe(b"\x00\x00")
            assert result.text == "" and result.language is None


async def test_eleven_v3_speaks_gujarati_and_english_without_changing_the_voice_account() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\x00\x00")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = ElevenLabsTTSProvider(SecretStr("test"), client=client, model_id="eleven_v3")
        for language in ("en", "gu"):
            assert [
                chunk
                async for chunk in provider.synthesize(
                    "Hello" if language == "en" else "નમસ્તે", language=language
                )
            ]
    assert [json.loads(request.content)["language_code"] for request in requests] == ["en", "gu"]
    assert all(json.loads(request.content)["model_id"] == "eleven_v3" for request in requests)
    assert requests[0].url == requests[1].url


@pytest.mark.parametrize("code", ["gu", "gu-IN", "gu_IN", "guj", "GUJ"])
def test_gujarati_codes_are_canonical_and_persistable(code: str) -> None:
    policy = LanguagePolicy(default_language=code, enabled_languages=[code])
    assert policy.default_language == "gu" and policy.enabled_languages == ["gu"]
    assert normalize_language(code) == "gu"
    assert LanguagePolicy.model_validate_json(policy.model_dump_json()) == policy


def test_gujarati_is_opt_in_and_aliases_cannot_duplicate_a_language() -> None:
    assert "gu" not in LanguagePolicy().enabled_languages
    assert default_settings("Clinic").receptionist.languages.enabled_languages == ["en"]
    with pytest.raises(ValidationError):
        LanguagePolicy(default_language="gu", enabled_languages=["gu", "guj"])


def test_text_detection_and_fixed_gujarati_policy() -> None:
    observation = observe_language(GUJARATI)
    assert observation is not None and observation.language == "gu"
    assert observe_language(GUJARATI, enabled_languages=("en",)) is None
    assert observe_language("હા", "gu") is None
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAgujarati", language_policy=gujarati_policy()
    )
    ConversationStateManager.process_caller_utterance(
        state, "Please speak in English now", detected_language="en"
    )
    assert state.language == "gu"


@pytest.mark.parametrize("answer", ["હા", "હાં!", "જી હા", "હા બુક કરો", "પુષ્ટિ કરું છું"])
def test_exact_gujarati_consent(answer: str) -> None:
    assert confirmation_answer(answer, "gu") is True


@pytest.mark.parametrize("answer", ["ના", "હા નહીં", "હા, પણ કાલે", "હા પણ સમય બદલો", "હા બુક ના કરો"])
def test_refusals_and_revisions_cannot_confirm(answer: str) -> None:
    assert confirmation_answer(answer, "gu") is not True
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAgujarati", language_policy=gujarati_policy()
    )
    ask_to_confirm(state, {"time": "10:00"}, "પુષ્ટિ કરો")
    ConversationStateManager.process_caller_utterance(state, answer)
    assert state.confirmation_state.status != ConfirmationStatus.CONFIRMED
    ConversationStateManager.process_caller_utterance(state, "હા")
    assert state.confirmation_state.status != ConfirmationStatus.CONFIRMED
    ask_to_confirm(state, {"time": "11:00"}, "નવા સમયની પુષ્ટિ કરો")
    ConversationStateManager.process_caller_utterance(state, "હા")
    assert state.confirmation_state.status == ConfirmationStatus.CONFIRMED


def test_gujarati_messages_are_complete_and_preserve_placeholders() -> None:
    assert MESSAGES["gu"].keys() == MESSAGES["en"].keys()
    assert SAFETY_MESSAGES["gu"].keys() == SAFETY_MESSAGES["en"].keys()
    for key, template in MESSAGES["gu"].items():
        expected = {name for _, name, _, _ in Formatter().parse(MESSAGES["en"][key]) if name}
        actual = {name for _, name, _, _ in Formatter().parse(template) if name}
        assert actual == expected
        assert any("\u0a80" <= char <= "\u0aff" for char in template)
    assert "ડૉક્ટર શાહ" in spoken("gu", "confirm", provider="ડૉક્ટર શાહ", offer="સમય ૧૦:૩૦")


def test_normalization_preserves_vowels_and_gujarati_digits() -> None:
    assert normalize_transcript("શ્વાસ લેવામાં તકલીફ, સવારે ૧૦:૩૦!") == ("શ્વાસ લેવામાં તકલીફ સવારે ૧૦ ૩૦")


@pytest.mark.parametrize(
    "utterance,decision",
    [
        ("મને અત્યારે શ્વાસ લેવામાં તકલીફ થાય છે", SafetyDecision.EMERGENCY_PROTOCOL),
        ("શ્વાસ લેવાતો નથી", SafetyDecision.EMERGENCY_PROTOCOL),
        ("મને છાતીમાં દુખાવો છે", SafetyDecision.EMERGENCY_PROTOCOL),
        ("મારા પિતા બેભાન છે", SafetyDecision.EMERGENCY_PROTOCOL),
        ("લોહી વહેવાનું બંધ થતું નથી", SafetyDecision.EMERGENCY_PROTOCOL),
        ("મેં વધુ પડતી ગોળીઓ લીધી છે", SafetyDecision.EMERGENCY_PROTOCOL),
        ("મારે મરી જવું છે", SafetyDecision.EMERGENCY_PROTOCOL),
        ("મને સ્ટાફ સાથે વાત કરાવો", SafetyDecision.TRANSFER),
        ("મને છાતીમાં દુખાવો નથી", SafetyDecision.CONTINUE),
        ("ગયા વર્ષે મને છાતીમાં દુખાવો હતો", SafetyDecision.CONTINUE),
        ("હાર્ટ એટેક શું છે", SafetyDecision.CONTINUE),
        ("મને છાતીમાં દુખાવો છે શું છે", SafetyDecision.EMERGENCY_PROTOCOL),
        ("મને છાતીમાં દુખાવો નથી પણ શ્વાસ લેવામાં તકલીફ છે", SafetyDecision.EMERGENCY_PROTOCOL),
        ("મને મુલાકાત બુક કરાવવી છે", SafetyDecision.CONTINUE),
    ],
)
def test_gujarati_safety_rules(utterance: str, decision: SafetyDecision) -> None:
    assert deterministic_rule(utterance).decision == decision


async def test_gujarati_safety_interrupt_clears_pending_confirmation() -> None:
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAgujarati", language_policy=gujarati_policy()
    )
    ask_to_confirm(state, {"time": "10:00"}, "પુષ્ટિ કરો")
    message = await SafetyEngine().evaluate(state, "મને છાતીમાં દુખાવો છે")
    assert message == SAFETY_MESSAGES["gu"]["emergency"]
    assert state.safety_state.is_emergency
    assert state.confirmation_state.status != ConfirmationStatus.CONFIRMED
    assert not state.awaiting_confirmation


def test_gujarati_appointment_time_uses_clinic_timezone_and_calendar_date() -> None:
    instant = datetime(2026, 10, 18, 23, 30, tzinfo=UTC)
    assert when(instant, "Asia/Kolkata", "gu") == "સોમવાર, 19 ઑક્ટોબર 2026, સવારે 5:00"
    assert when(instant, "Asia/Kolkata") == "Mon 19 Oct, 5:00 AM"


def test_gujarati_prompt_keeps_tool_policy_and_script_instruction() -> None:
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAgujarati", language_policy=gujarati_policy()
    )
    session = Session(
        state.tenant_id, state.call_id, "test", uuid4(), default_settings("Clinic"), frozenset()
    )
    prompt = system_prompt(session, state)
    assert '"conversation_language": "gu"' in prompt
    assert "Gujarati script" in prompt and "Gujarati digits" in prompt
    assert "The server asks the person to confirm" in prompt


def test_gujarati_provider_configuration_validation(settings: Settings) -> None:
    assert DeepgramConfig(model="nova-3", language="gu").language == "gu"
    with pytest.raises(ValidationError):
        DeepgramConfig(model="nova-2", language="gu")
    config = settings.model_copy(
        update={
            "stt_provider": "deepgram",
            "deepgram_model": "nova-3",
            "deepgram_language": "gu",
            "tts_provider": "azure",
        }
    )
    validate_gujarati_voice(config, gujarati_policy(), None, None)
    for overrides in (
        {"deepgram_language": "multi"},
        {"deepgram_model": "nova-2"},
        {"tts_provider": "elevenlabs"},
    ):
        with pytest.raises(GujaratiVoiceConfigurationError):
            validate_gujarati_voice(
                config.model_copy(update=overrides), gujarati_policy(), None, None
            )
    with pytest.raises(GujaratiVoiceConfigurationError):
        validate_gujarati_voice(
            config,
            LanguagePolicy(default_language="gu", enabled_languages=["gu", "hi"]),
            None,
            None,
        )


def test_gujarati_access_codes_are_canonical_but_still_strict() -> None:
    assert IdentifyPatient(access_code="૦૧૨૩૪૫૬૭").access_code == "01234567"
    assert normalize_access_code("૧૨3૪૫6૭૮") == "12345678"
    for value in ("૧૨૩૪૫૬૭", "૧૨૩૪૫૬૭૮૯", "૧૨૩૪abcd", "૧૨૩૪ ૫૬૭૮"):
        with pytest.raises(ValidationError):
            IdentifyPatient(access_code=value)


async def test_gujarati_code_is_consumed_by_the_server_before_model_dispatch() -> None:
    class CodeFlow(AppointmentFlow):
        def __init__(self) -> None:
            self.received: str | None = None

        async def identify(
            self, session: Session, state: ConversationState, payload: BaseModel
        ) -> BaseModel:
            self.received = IdentifyPatient.model_validate(payload.model_dump()).access_code
            return Outcome(status="identified")

    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAgujarati", language_policy=gujarati_policy()
    )
    ConversationStateManager.process_caller_utterance(state, "૦૧૨૩૪૫૬૭")
    session = Session(
        state.tenant_id,
        state.call_id,
        "voice",
        uuid4(),
        default_settings("Clinic"),
        frozenset({"identify_patient"}),
        provider_call_id="CAgujarati",
    )
    flow = CodeFlow()
    result = await flow.before_turn(session, state, "૦૧૨૩૪૫૬૭")
    assert result is not None and result.tool == "identify_patient"
    assert flow.received == "01234567"
    assert result.response == MESSAGES["gu"]["identified"]
    assert state.last_caller_utterance == "[call access code]"


@pytest.mark.parametrize(
    "text,secret,marker",
    [
        ("મારો કોડ ૧૨૩૪૫૬૭૮", "૧૨૩૪૫૬૭૮", "[call access code]"),
        ("મારો નંબર +૯૧૯૮૭૬૫૪૩૨૧૦", "૯૧૯૮૭૬૫૪૩૨૧૦", "[phone number]"),
        ("જન્મ તારીખ ૦૧/૦૨/૧૯૯૦", "૦૧/૦૨/૧૯૯૦", "[date of birth]"),
        ("ખાતા નંબર ૧૨૩૪", "૧૨૩૪", "[account identifier]"),
    ],
)
def test_gujarati_identifiers_are_redacted(text: str, secret: str, marker: str) -> None:
    assert secret not in redact_transcript(text)
    assert marker in redact_transcript(text)
    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAgujarati", language_policy=gujarati_policy()
    )
    ConversationStateManager.process_caller_utterance(state, text)
    assert secret not in state.transcript_turns[-1].text


async def test_gujarati_access_code_never_reaches_semantic_classifier() -> None:
    class SpyClassifier:
        calls = 0

        async def classify(self, utterance: str, context_hint: str) -> SafetySignal:
            self.calls += 1
            raise AssertionError("Access code reached classifier")

    state = ConversationStateManager.create_initial_state(
        uuid4(), uuid4(), "CAgujarati", language_policy=gujarati_policy()
    )
    classifier = SpyClassifier()
    await SafetyEngine(classifier).evaluate(state, "૧૨૩૪૫૬૭૮")
    assert classifier.calls == 0


def test_effective_tenant_providers_override_platform_and_do_not_mutate_it(
    settings: Settings,
) -> None:
    config = settings.model_copy(update={"stt_provider": "deepgram", "tts_provider": "elevenlabs"})
    tenant = uuid4()
    stt = ResolvedIntegration(
        tenant,
        uuid4(),
        uuid4(),
        "azure_speech",
        "stt",
        "primary",
        None,
        {"region": "eastus", "language": "gu-IN"},
        {"speech_key": SecretStr("test")},
    )
    tts = ResolvedIntegration(
        tenant,
        uuid4(),
        uuid4(),
        "azure_speech",
        "tts",
        "primary",
        None,
        {"region": "eastus", "voice": "en-US-JennyNeural", "hindi_voice": "hi-IN-SwaraNeural"},
        {"speech_key": SecretStr("test")},
    )
    validate_gujarati_voice(config, gujarati_policy(), stt, tts)
    assert config.stt_provider == "deepgram" and config.tts_provider == "elevenlabs"
    with pytest.raises(GujaratiVoiceConfigurationError):
        validate_gujarati_voice(config, gujarati_policy(), stt, None)


async def test_gujarati_speech_http_contracts_and_unsupported_voice() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "stt.speech" in str(request.url):
            return httpx.Response(
                200, json={"RecognitionStatus": "Success", "DisplayText": GUJARATI}
            )
        if "api.deepgram" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "results": {
                        "channels": [
                            {
                                "alternatives": [
                                    {
                                        "transcript": GUJARATI,
                                        "confidence": 0.95,
                                        "languages": ["gu"],
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, content=b"\x00\x00")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        azure = AzureTTSProvider(SecretStr("test"), client=client)
        assert [chunk async for chunk in azure.synthesize("નમસ્તે", language="gu")]
        azure_stt = AzureSTTProvider(
            SecretStr("test"), client=client, language_resolver=lambda: "gu-IN"
        )
        assert (await azure_stt.transcribe(b"\x00\x00")).text == GUJARATI
        deepgram = DeepgramSTTProvider(
            SecretStr("test"), model="nova-3", language="gu", client=client
        )
        assert (await deepgram.transcribe(b"\x00\x00")).text == GUJARATI
        eleven = ElevenLabsTTSProvider(SecretStr("test"), client=client)
        with pytest.raises(TTSPermanentError, match="Gujarati"):
            _ = [chunk async for chunk in eleven.synthesize("નમસ્તે", language="gu")]
    assert b"gu-IN-DhwaniNeural" in requests[0].content
    assert requests[0].headers["X-Microsoft-OutputFormat"] == "raw-8khz-16bit-mono-pcm"
    assert requests[1].url.params["language"] == "gu-IN"
    assert requests[2].url.params["language"] == "gu"
    assert requests[2].url.params["model"] == "nova-3"
    assert len(requests) == 3


async def test_fixed_recognition_still_speaks_gujarati_without_changing_other_calls() -> None:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    for code in ("gu", "en"):
        state = ConversationStateManager.create_initial_state(
            uuid4(),
            uuid4(),
            "CAgujarati",
            language_policy=LanguagePolicy(default_language=code, enabled_languages=[code]),
        )
        tts = MockTTSProvider(total_chunks=1)
        pipeline = VoicePipeline(
            "MZgujarati",
            MockSTTProvider(),
            tts,
            send,
            conversation_state=state,
            multilingual_enabled=False,
        )
        await pipeline.start()
        try:
            assert await pipeline.speak("નમસ્તે" if code == "gu" else "Hello")
            assert tts.last_language == code
        finally:
            await pipeline.close()
    assert any(frame["event"] == "media" for frame in sent)
