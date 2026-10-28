import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from pydantic import SecretStr

from app.ai.llm.base import LLMProvider, UsageMeter
from app.ai.llm.models import Effort, Job, ModelChoiceError, ModelProfile, model_jobs, profile
from app.core.settings import Settings

logger = logging.getLogger(__name__)

# Also the deadline the Gemini SDK sends to Google, which refuses a short one (5 s
# came back 400 INVALID_ARGUMENT); a call's own limit is kept by the gateway.
TIMEOUT_SECONDS = 10
# Thinking tokens count against this budget, so it leaves room for the reply.
MAX_OUTPUT_TOKENS = 2048


@dataclass
class ModelSet:
    """The models one conversation uses, one per job, all on one provider account."""

    # Replies: the text model in messages, the call model on a call.
    reply: LLMProvider
    # The safety check, reading a yes/no, the running call summary.
    quick: LLMProvider
    # One last attempt when the reply model keeps failing; from another maker.
    backup: LLMProvider
    # Which model account is in use, for its circuit breaker.
    account: str
    meter: UsageMeter = field(default_factory=UsageMeter)
    # A model handed in from outside (a test's) is closed by whoever made it.
    owned_by_caller: bool = False

    @property
    def owned(self) -> tuple[LLMProvider, ...]:
        if self.owned_by_caller:
            return ()
        return tuple({id(model): model for model in (self.reply, self.quick, self.backup)}.values())

    async def close(self) -> None:
        for model in self.owned:
            with suppress(Exception):
                await model.close()


def _provider(
    model: ModelProfile,
    api_key: SecretStr,
    effort: Effort | None,
    *,
    fastest: bool,
    meter: UsageMeter | None,
) -> LLMProvider:
    if model.provider == "gemini":
        from app.ai.llm.gemini import GeminiLLMProvider

        return GeminiLLMProvider(
            api_key,
            model.slug,
            TIMEOUT_SECONDS,
            MAX_OUTPUT_TOKENS,
            thinking_level=None if effort == "none" else effort,
            priority=fastest and model.priority_tier,
            meter=meter,
        )
    from app.ai.llm.openrouter import OpenRouterLLMProvider

    return OpenRouterLLMProvider(
        api_key,
        model.slug,
        TIMEOUT_SECONDS,
        MAX_OUTPUT_TOKENS,
        reasoning_effort=effort,
        fastest=fastest,
        meter=meter,
    )


def provider_for(slug: str, api_key: SecretStr, *, voice: bool = False) -> LLMProvider:
    """One catalog model on its own, set up as for a call or a text reply (for the
    model comparison run). Raises ModelChoiceError for a model not in the catalog."""
    model = profile(slug)
    if model is None:
        raise ModelChoiceError(f"{slug} is not in the model catalog")
    return _provider(
        model, api_key, model.lowest_effort if voice else "low", fastest=voice, meter=None
    )


def open_models(
    provider: str, api_key: SecretStr, config: dict[str, Any], account: str, *, voice: bool
) -> ModelSet:
    """The models for one conversation from a model connection's settings.

    On a call every second of thinking is a second of silence, so replies, checks and
    the backup think as little as each model allows and go to its quickest endpoint.
    A saved choice the catalog no longer allows (a model was withdrawn) is replaced by
    the defaults rather than leaving the conversation without a model.
    """
    try:
        jobs = model_jobs(provider, config)
    except ModelChoiceError as error:
        logger.warning("model_choice_outdated", extra={"field": error.field, "account": account})
        jobs = model_jobs(provider, {})
    chosen: Effort | None = config.get("thinking_level", "low")
    meter = UsageMeter()

    def build(job: Job) -> LLMProvider:
        model = jobs.for_job(job)
        quickest = voice or job == "quick"
        effort = model.lowest_effort if quickest else chosen
        # Quick checks report their usage here; replies report it with their turn.
        return _provider(
            model, api_key, effort, fastest=voice, meter=meter if job == "quick" else None
        )

    return ModelSet(
        build("voice" if voice else "text"), build("quick"), build("backup"), account, meter
    )


def platform_models(settings: Settings, *, voice: bool = False) -> ModelSet | None:
    """The platform's own model account, as a platform administrator configured it, or
    None when it has no key."""
    key = settings.platform_llm_key
    if not key.get_secret_value():
        return None
    return open_models(settings.llm_provider, key, settings.llm_config, "platform", voice=voice)
