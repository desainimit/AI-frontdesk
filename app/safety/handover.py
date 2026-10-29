"""Putting a caller through only when they agree.

The receptionist first tries to help. When someone asks for a person, or it cannot help
with what they need, it offers to put them through (or, when nobody can take the call,
to leave a message for the team) and waits for the answer. A clear yes, or asking for a
person a second time, puts them through; a no carries on the conversation. Anything
else leaves the offer open for two more turns, for the model to follow up.
"""

import re
from datetime import UTC, datetime

from app.conversation.state import ConversationState, SafetyDecision
from app.languages import all_packs
from app.safety.rules import HUMAN_REQUEST
from app.settings.schemas import ClinicConfiguration, TransferTarget

OFFER_TURNS = 2
# "Yes, put me through", in any language.
_AGREE = re.compile(
    "|".join(item.transfer_agree for item in all_packs() if item.transfer_agree), re.IGNORECASE
)

# A reply that starts with "no" is a no, whatever follows ("No, can you do it again?").
_LEADING_NO = re.compile(
    r"^\W*(?:" + "|".join(item.transfer_no for item in all_packs()) + r")(?![\wऀ-૿])",
    re.IGNORECASE,
)


def targets_now(
    configuration: ClinicConfiguration, state: ConversationState
) -> list[TransferTarget]:
    """Where the caller can be put through right now. Once a transfer went unanswered,
    nowhere: the request is already with the team."""
    if state.safety_state.transfer_attempted:
        return []
    return configuration.transfer_targets_now(datetime.now(UTC))


def choose(targets: list[TransferTarget], wanted: str | None) -> TransferTarget | None:
    """The target the model named, else the default (the first one)."""
    if not targets:
        return None
    if wanted:
        key = wanted.strip().casefold()
        for target in targets:
            if target.name.casefold() == key:
                return target
        for target in targets:
            if key in target.name.casefold() or (key and key in target.description.casefold()):
                return target
    return targets[0]


def offer(state: ConversationState, target: TransferTarget | None, note: str | None) -> None:
    # The newest question is the one the caller answers: a booking that was waiting for
    # a yes is dropped, and the model proposes it again if they still want it.
    state.appointment_operation = None
    state.confirmation_state.reset()
    safety = state.safety_state
    safety.transfer_offered = True
    safety.offered_turn = state.utterance_count
    safety.transfer_target = target.name if target else None
    if note:
        safety.handoff_note = note.strip()[:500]


def pending(state: ConversationState) -> bool:
    safety = state.safety_state
    return safety.transfer_offered and state.utterance_count - safety.offered_turn <= OFFER_TURNS


def withdraw(state: ConversationState) -> None:
    state.safety_state.transfer_offered = False


def consent(utterance: str, answer: bool | None) -> bool | None:
    """Yes, no, or neither, to "shall I put you through?". `answer` is the general reading
    of the reply as yes or no."""
    if HUMAN_REQUEST.search(utterance):
        return True
    if _LEADING_NO.search(utterance):
        return False
    if _AGREE.search(utterance):
        return True
    return answer


def agree(state: ConversationState, *, can_transfer: bool) -> SafetyDecision:
    """The caller agreed: put them through, or take the message when nobody can answer."""
    safety = state.safety_state
    safety.transfer_offered = False
    safety.decision = SafetyDecision.TRANSFER if can_transfer else SafetyDecision.ESCALATE
    safety.reason = safety.reason or "human_requested"
    safety.source = safety.source or "caller"
    safety.triggered_at = datetime.now(UTC)
    return safety.decision
