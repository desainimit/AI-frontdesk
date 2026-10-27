"""The call keeps its language until the caller really changes it (27 Oct, 18:13 call)."""

from uuid import uuid4

from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConversationState


def call() -> ConversationState:
    return ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CA" + "0" * 32)


def say(state: ConversationState, text: str, label: str) -> None:
    ConversationStateManager.process_caller_utterance(state, text, detected_language=label)


def test_one_misheard_english_line_does_not_switch_a_hindi_call() -> None:
    state = call()
    say(state, "हमारे appointment का timing change करवाना है", "hi")
    assert state.language == "hi"
    # Hindi misheard as English, then Hindi the recogniser labelled English.
    say(state, "That's what I get now.", "en")
    say(state, "दस बजे का चाहिए.", "en")
    say(state, "Mares मुझे साढ़े दस की appointment चाहिए.", "en")
    assert state.language == "hi"


def test_the_caller_switching_language_is_followed() -> None:
    state = call()
    say(state, "मुझे कल का appointment चाहिए", "hi")
    say(state, "Can we speak in English please?", "en")
    say(state, "I would like tomorrow morning.", "en")
    assert state.language == "en"
