"""The models the product may use, what each costs and can do, and which jobs it does.

One list for every provider account: the platform's and a clinic's own. A model that is
not here cannot be chosen, so a new one is added here once it has been checked: it must
have zero-data-retention endpoints on OpenRouter (patient conversations), take tools and
a JSON schema, and its reasoning switch must be known.

Prices are US dollars per million tokens on the endpoints these requests may use
(OpenRouter's zero-retention list, checked 2026-10-28). They are used only when the
provider does not report a request's cost itself.
"""

from dataclasses import dataclass
from typing import Any, Literal

Job = Literal["text", "voice", "quick", "backup"]
JOBS: tuple[Job, ...] = ("text", "voice", "quick", "backup")
# Where each job's model is kept in a model connection's settings.
JOB_FIELDS: dict[Job, str] = {
    "text": "model",
    "voice": "voice_model",
    "quick": "quick_model",
    "backup": "backup_model",
}
Effort = Literal["none", "minimal", "low", "medium", "high"]
Tier = Literal["recommended", "low_cost", "quality"]


@dataclass(frozen=True)
class ModelProfile:
    slug: str
    provider: Literal["openrouter", "gemini"]
    label: str
    maker: str
    price_in: float
    price_out: float
    price_cached: float
    # The least thinking the model allows: what calls and quick checks ask for.
    lowest_effort: Effort
    # Whether the provider has a faster, pricier lane for calls ("priority").
    priority_tier: bool
    # "explicit": the model caches the fixed part of the instructions only when asked.
    caching: Literal["implicit", "explicit"]
    jobs: frozenset[Job]
    tier: Tier
    # Measured on the real call prompt: first words within about two seconds.
    call_tested: bool = False

    def cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        fresh = max(input_tokens - cached_tokens, 0)
        cached, written = cached_tokens * self.price_cached, output_tokens * self.price_out
        return (fresh * self.price_in + cached + written) / 1_000_000


_ALL: frozenset[Job] = frozenset(JOBS)
_NOT_CALLS: frozenset[Job] = frozenset({"text", "quick", "backup"})

MODELS: tuple[ModelProfile, ...] = (
    ModelProfile(
        "google/gemini-3.5-flash-lite",
        "openrouter",
        "Gemini 3.5 Flash-Lite",
        "google",
        0.30,
        2.50,
        0.03,
        "minimal",
        True,
        "implicit",
        _ALL,
        "recommended",
        call_tested=True,
    ),
    ModelProfile(
        "anthropic/claude-haiku-5.5",
        "openrouter",
        "Claude Haiku 5.5",
        "anthropic",
        0.10,
        0.50,
        0.01,
        "none",
        False,
        "explicit",
        _ALL,
        "recommended",
    ),
    ModelProfile(
        "openai/gpt-6-luna",
        "openrouter",
        "GPT-6 Luna",
        "openai",
        0.10,
        0.50,
        0.01,
        "minimal",
        False,
        "implicit",
        _ALL,
        "recommended",
    ),
    ModelProfile(
        "google/gemini-3.8-flash",
        "openrouter",
        "Gemini 3.8 Flash",
        "google",
        0.75,
        3.75,
        0.075,
        "minimal",
        True,
        "implicit",
        # Thought for 4.6 s before its first words at "minimal" (26 Oct): too slow for calls.
        frozenset({"text", "backup"}),
        "quality",
    ),
    ModelProfile(
        "google/gemini-3.1-flash-lite",
        "openrouter",
        "Gemini 3.1 Flash-Lite",
        "google",
        0.25,
        1.50,
        0.025,
        "minimal",
        True,
        "implicit",
        _ALL,
        "low_cost",
    ),
    ModelProfile(
        "deepseek/deepseek-v4.1-flash",
        "openrouter",
        "DeepSeek V4.1 Flash",
        "deepseek",
        0.20,
        1.00,
        0.02,
        "none",
        False,
        "implicit",
        _ALL,
        "low_cost",
    ),
    ModelProfile(
        "mistralai/mistral-small-2603",
        "openrouter",
        "Mistral Small 26.03",
        "mistral",
        0.15,
        0.60,
        0.015,
        "none",
        False,
        "implicit",
        _ALL,
        "low_cost",
    ),
    ModelProfile(
        "z-ai/glm-5.3-flash",
        "openrouter",
        "GLM 5.3 Flash",
        "z-ai",
        0.15,
        0.50,
        0.036,
        "none",
        False,
        "implicit",
        _NOT_CALLS,
        "low_cost",
    ),
    # Google's own API, for a clinic or platform that connects Gemini directly.
    ModelProfile(
        "gemini-3.5-flash-lite",
        "gemini",
        "Gemini 3.5 Flash-Lite",
        "google",
        0.30,
        2.50,
        0.03,
        "minimal",
        True,
        "implicit",
        _ALL,
        "recommended",
        call_tested=True,
    ),
    ModelProfile(
        "gemini-3.8-flash",
        "gemini",
        "Gemini 3.8 Flash",
        "google",
        0.75,
        3.75,
        0.075,
        "minimal",
        True,
        "implicit",
        frozenset({"text", "backup"}),
        "quality",
    ),
    ModelProfile(
        "gemini-3.1-flash-lite",
        "gemini",
        "Gemini 3.1 Flash-Lite",
        "google",
        0.25,
        1.50,
        0.025,
        "minimal",
        True,
        "implicit",
        _ALL,
        "low_cost",
    ),
)
_BY_SLUG = {model.slug: model for model in MODELS}

# What a new connection uses until someone chooses otherwise. Calls keep the model that
# was measured on them; the backup comes from another maker wherever the account allows,
# so one company's outage does not silence the receptionist.
DEFAULTS: dict[str, dict[Job, str]] = {
    "openrouter": {
        "text": "google/gemini-3.8-flash",
        "voice": "google/gemini-3.5-flash-lite",
        "quick": "google/gemini-3.5-flash-lite",
        "backup": "anthropic/claude-haiku-5.5",
    },
    "gemini": {
        "text": "gemini-3.8-flash",
        "voice": "gemini-3.5-flash-lite",
        "quick": "gemini-3.5-flash-lite",
        "backup": "gemini-3.1-flash-lite",
    },
}


class ModelChoiceError(ValueError):
    """A model choice the catalog does not allow; the message is safe to show."""

    def __init__(self, message: str, field: str = "model") -> None:
        super().__init__(message)
        # The connection setting it is about, so a form can point at it.
        self.field = field


def profile(slug: str) -> ModelProfile | None:
    return _BY_SLUG.get(slug)


def models_for(provider: str) -> tuple[ModelProfile, ...]:
    return tuple(model for model in MODELS if model.provider == provider)


@dataclass(frozen=True)
class ModelJobs:
    text: ModelProfile
    voice: ModelProfile
    quick: ModelProfile
    backup: ModelProfile

    def for_job(self, job: Job) -> ModelProfile:
        return getattr(self, job)


def model_jobs(provider: str, config: dict[str, Any]) -> ModelJobs:
    """The model for each job from a connection's settings, with the defaults for any
    job not chosen. Raises ModelChoiceError for a choice the catalog does not allow."""
    defaults = DEFAULTS.get(provider)
    if defaults is None:
        raise ModelChoiceError("Unsupported AI model provider", "provider")
    chosen: dict[Job, ModelProfile] = {}
    for job in JOBS:
        slug = str(config.get(JOB_FIELDS[job]) or defaults[job])
        model = profile(slug)
        if model is None or model.provider != provider:
            raise ModelChoiceError(
                f"{slug} is not an available model for this account", JOB_FIELDS[job]
            )
        if job not in model.jobs:
            raise ModelChoiceError(
                f"{model.label} cannot be used for {JOB_LABELS[job]}", JOB_FIELDS[job]
            )
        chosen[job] = model
    backup = chosen["backup"]
    if backup.slug in {chosen["text"].slug, chosen["voice"].slug}:
        raise ModelChoiceError("Choose a backup model different from the main ones", "backup_model")
    # Through OpenRouter every maker is available, so the backup must not share the
    # call model's maker: an outage at one company then leaves the other answering.
    if provider == "openrouter" and backup.maker == chosen["voice"].maker:
        raise ModelChoiceError("Choose a backup model from a different company", "backup_model")
    return ModelJobs(**chosen)


JOB_LABELS: dict[Job, str] = {
    "text": "text replies",
    "voice": "call replies",
    "quick": "quick checks",
    "backup": "the backup model",
}
