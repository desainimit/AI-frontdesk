"""Everything the receptionist says or listens for in one language.

One pack per language (app/languages/packs/<code>.py), each with every field filled:
tests/test_language_packs.py fails when a pack lacks a message, a placeholder or a word
list, so a language is either complete or cannot be offered. Nothing falls back to
English mid-conversation.

Word lists hold this language's words only. Where the product listens for something
in any language (yes and no, "bye", an emergency), the lists of every pack are joined:
a Hindi caller saying "okay" is still understood.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SafetyWords:
    """Regular-expression parts (alternatives, no outer group) for the safety rules.

    Latin-script parts are matched as whole words; others as written. Each is this
    language's way of saying it, for the categories in app/safety/rules.py.
    """

    breathing: str
    chest: str
    neurologic: str
    bleeding: str
    overdose: str
    self_harm: str
    human_request: str
    ambiguous: str
    # Said at the start: an event in the past or a hypothetical ("last year", "what if").
    historical: str
    current: str
    informational: str
    scheduling: str
    symptom: str
    # A negation just before a symptom ("no chest pain").
    negation: str
    worsening: str


@dataclass(frozen=True)
class ClockWords:
    """How a time of day is said where it is said the Indian way (साढ़े पाँच, सवा नौ)."""

    hours: tuple[str, ...]
    morning: str
    afternoon: str
    evening: str
    night: str
    oclock: str
    half: str
    quarter: str
    to: str
    one_half: str
    two_half: str
    minutes: str


@dataclass(frozen=True)
class LanguagePack:
    code: str
    # The fixed lines the server says or writes itself (app/ai/wording.py).
    messages: dict[str, str]
    # What a receptionist says while looking something up.
    fillers: tuple[str, ...]
    # Said the moment the caller stops talking, and after a question from the caller.
    acks: tuple[str, ...]
    question_acks: tuple[str, ...]
    # Openers that would repeat an acknowledgement ("Sure,"), as regex alternatives.
    openers: str
    # Words of someone hurt or upset, after which no breezy acknowledgement is said.
    serious: str
    # What is said while a tool runs, by tool.
    narration: dict[str, tuple[str, ...]]
    # Putting a caller through to a person (app/safety/handover_messages.py).
    handover: dict[str, str]
    # Safety replies (app/safety/messages.py).
    safety: dict[str, str]
    emergency_with_number: str
    crisis: str
    safety_words: SafetyWords
    # "Yes, put me through" and a leading "no" to that offer, as regex alternatives.
    transfer_agree: str
    transfer_no: str
    # Listening: short sounds that are not a turn ("mm", "हाँ").
    backchannel: frozenset[str]
    # A goodbye ("bye", "thank you"), and words that say more is coming ("but", "also").
    closing: frozenset[str]
    carry_on: frozenset[str]
    # "That's all", "nothing else": the caller is done.
    done: frozenset[str]
    # Regex alternatives: a goodbye phrase, and "anything else?" asked by the receptionist.
    ending: str
    anything_else: str
    # Words a sentence does not end on ("and", "aur"): the caller is still speaking.
    unfinished: frozenset[str]
    # Yes, no and "that is not right" to a question the server asked.
    agree: frozenset[str]
    agreement_idioms: tuple[str, ...]
    hesitation: frozenset[str]
    refuse: frozenset[str]
    soft_no: frozenset[str]
    not_right: frozenset[str]
    # How this language names each language a caller may ask for, by code.
    language_names: dict[str, tuple[str, ...]]
    # Words that make naming a language a request to speak it ("Hindi mein bolo").
    asking: frozenset[str]
    # Texts that stop and restart messages, besides the carriers' English words.
    stop_words: frozenset[str]
    start_words: frozenset[str]
    weekdays: tuple[str, ...]
    months: tuple[str, ...]
    # "today", "tomorrow"
    today_tomorrow: tuple[str, str]
    # The instruction on how to write in this language, said last in the prompt.
    script_rule: str
    # A new clinic's wording in this language, and how a date is written in a text:
    # greeting and unknown_answer ({clinic}); confirmation_text, reminder_text and
    # cancellation_text ({{patient_name}} and the other message placeholders); the
    # email subjects ({clinic}); number_suffix ({number}); date (see templates.when).
    clinic_texts: dict[str, str]
    clock: ClockWords | None = None
    # Spoken digits 0-9 ("zero"..."nine") as callers say them, for appointment numbers.
    digit_words: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    # Everyday words of a language usually written in another script, as they come out
    # in Latin letters ("mujhe", "chahiye"), for telling it apart from English.
    latin_words: frozenset[str] = frozenset()
    # Regex alternatives: the receptionist's own goodbye ("take care").
    farewell: str = ""
