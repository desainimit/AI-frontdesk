"""Putting a caller through only when they agree.

The receptionist first tries to help. When someone asks for a person, or it cannot help
with what they need, it offers to put them through (or, when nobody can take the call,
to leave a message for the team) and waits for the answer. A clear yes, or asking for a
person a second time, puts them through; a no carries on the conversation. Anything
else leaves the offer open for two more turns, for the model to follow up.
"""

import re
from datetime import UTC, datetime

from app.conversation.confirmation import confirmation_answer
from app.conversation.state import ConversationState, SafetyDecision
from app.safety.rules import HUMAN_REQUEST
from app.settings.schemas import ClinicConfiguration, TransferTarget

OFFER_TURNS = 2
_AGREE = re.compile(
    r"\b(?:put me through|connect me|transfer me|go ahead|yes please|please do|do it|"
    r"(?:haan|ha|ji)\s+(?:kar|jod|connect)\w*|(?:jod|connect)\s+(?:kar|kijiye|karo|do|dijiye)\w*)\b|"
    r"(?:जोड़|कनेक्ट)\s*(?:दो|दीजिए|कर\s*दो|कर\s*दीजिए)|જોડી\s*(?:દો|આપો)",
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


def consent(utterance: str, language: str) -> bool | None:
    """Yes, no, or neither, to "shall I put you through?"."""
    if _AGREE.search(utterance) or HUMAN_REQUEST.search(utterance):
        return True
    return confirmation_answer(utterance, language)


def agree(state: ConversationState, *, can_transfer: bool) -> SafetyDecision:
    """The caller agreed: put them through, or take the message when nobody can answer."""
    safety = state.safety_state
    safety.transfer_offered = False
    safety.decision = SafetyDecision.TRANSFER if can_transfer else SafetyDecision.ESCALATE
    safety.reason = safety.reason or "human_requested"
    safety.source = safety.source or "caller"
    safety.triggered_at = datetime.now(UTC)
    return safety.decision
