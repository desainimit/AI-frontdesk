from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from app.conversation.manager import ConversationStateManager
from app.conversation.state import ConfirmationStatus, IdentifierType, LanguageState
from app.conversation.store import ConversationStoreError, RedisConversationStateStore
from tests.confirmations import ask_to_confirm


def new_state():
    return ConversationStateManager.create_initial_state(uuid4(), uuid4(), "CAtest")


@pytest.mark.parametrize(
    "reply",
    [
        "yes, don't book",
        "no yes",
        "yes but not tomorrow",
        "okay cancel it",
        "don't confirm",
        "yes nahi",
    ],
)
def test_negation_never_confirms(reply: str) -> None:
    state = new_state()
    ask_to_confirm(state, {"slot": "10am"}, "Confirm?")
    ConversationStateManager.process_caller_utterance(state, reply)
    assert state.confirmation_state.status == ConfirmationStatus.DENIED


@pytest.mark.parametrize("reply", ["yes but tomorrow", "maybe yes", "sure if it is free"])
def test_ambiguous_answer_never_confirms(reply: str) -> None:
    state = new_state()
    ask_to_confirm(state, {"slot": "10am"}, "Confirm?")
    ConversationStateManager.process_caller_utterance(state, reply)
    assert state.confirmation_state.status != ConfirmationStatus.CONFIRMED


def test_confirmation_copies_parameters_and_records_expiry() -> None:
    state = new_state()
    params = {"nested": {"slot": "10am"}}
    state.confirmation_state.request("book", params, "Confirm?")
    params["nested"]["slot"] = "11am"
    confirmed = state.confirmation_state.confirm()
    assert confirmed["nested"]["slot"] == "10am"
    confirmed["nested"]["slot"] = "12pm"
    assert state.confirmation_state.action_params["nested"]["slot"] == "10am"
    state.confirmation_state.request("book", {}, "Confirm?")
    state.confirmation_state.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert not state.confirmation_state.is_awaiting
    assert state.confirmation_state.status == ConfirmationStatus.EXPIRED


def test_identity_attempt_limit_cannot_be_bypassed() -> None:
    identity = new_state().identity_state
    identity.start_collecting(IdentifierType.OTP)
    for _ in range(3):
        identity.record_attempt(False)
    with pytest.raises(ValueError):
        identity.start_collecting(IdentifierType.PHONE)
    with pytest.raises(ValueError):
        identity.record_attempt(True, uuid4())
    assert not identity.is_verified
    fresh = new_state().identity_state
    with pytest.raises(ValueError):
        fresh.record_attempt(True)
    assert not fresh.is_verified


def test_language_single_high_confidence_word_and_hinglish_normalization() -> None:
    language = LanguageState(primary_language="hi")
    language.record_utterance("en", 0.99)
    assert language.primary_language == "hi"
    language.record_utterance("en", 0.99)
    assert language.primary_language == "en"
    language.record_utterance("hi-Latn", 0.9)
    language.record_utterance("hi-Latn", 0.9)
    assert language.primary_language == "hi-Latn"
    language.record_utterance("en", 0.1, threshold=0.8)
    assert language.primary_language == "hi-Latn"


def test_nested_call_scope_is_validated() -> None:
    state = new_state()
    state.call_state.tenant_id = uuid4()
    with pytest.raises(ValidationError):
        type(state).model_validate(state.model_dump())


async def test_redis_corrupt_state_and_outage_fail_closed_without_secrets() -> None:
    client = AsyncMock()
    store = RedisConversationStateStore(client)
    state = new_state()
    client.get.return_value = state.model_dump_json()
    with pytest.raises(ConversationStoreError):
        await store.get(uuid4(), state.call_id)
    client.get.return_value = '{"private": "do-not-log"}'
    with pytest.raises(ConversationStoreError) as error:
        await store.get(state.tenant_id, state.call_id)
    assert "do-not-log" not in str(error.value)
    client.get.side_effect = RedisConnectionError("redis://private-password@host")
    with pytest.raises(ConversationStoreError) as error:
        await store.get(state.tenant_id, state.call_id)
    assert "private-password" not in str(error.value)
