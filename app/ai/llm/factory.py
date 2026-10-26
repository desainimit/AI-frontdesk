from typing import Any

from pydantic import SecretStr

from app.ai.llm.base import LLMProvider
from app.core.settings import Settings

# Also the deadline the Gemini SDK sends to Google, which refuses a short one (5 s
# came back 400 INVALID_ARGUMENT); a call's own limit is kept by the gateway.
TIMEOUT_SECONDS = 10
# Thinking tokens count against this budget, so it leaves room for the reply.
MAX_OUTPUT_TOKENS = 2048
# The quicker, cheaper model: the safety check, summaries, and one last attempt at a
# reply when the main model keeps failing.
LIGHT_MODELS = {"gemini": "gemini-3.5-flash-lite", "openrouter": "google/gemini-3.5-flash-lite"}


# On a call every second of thinking is a second of silence, so voice turns think as
# little as the model allows and go to the quickest endpoint.
VOICE_REASONING = "minimal"
# Replies on a call come from the light model too. Measured on the real call prompt
# (2026-10-26): Flash still thought ~250 tokens at "minimal" and took 4.6 s to its
# first output; Flash-Lite thought none and spoke in 1.5 to 2 s, in every language the
# clinic offers, for a third of the price. Text conversations keep the main model.
VOICE_MODELS = LIGHT_MODELS


def _provider(
    name: str, api_key: SecretStr, model: str, reasoning: Any = "low", *, voice: bool = False
) -> LLMProvider:
    if voice:
        reasoning = VOICE_REASONING
    if name == "gemini":
        from app.ai.llm.gemini import GeminiLLMProvider

        return GeminiLLMProvider(
            api_key,
            model,
            TIMEOUT_SECONDS,
            MAX_OUTPUT_TOKENS,
            thinking_level=reasoning,
            priority=voice,
        )
    if name == "openrouter":
        from app.ai.llm.openrouter import OpenRouterLLMProvider

        return OpenRouterLLMProvider(
            api_key,
            model,
            TIMEOUT_SECONDS,
            MAX_OUTPUT_TOKENS,
            reasoning_effort=reasoning,
            fastest=voice,
        )
    raise ValueError("Unsupported LLM provider")


def build_llm_provider(
    settings: Settings, *, light: bool = False, voice: bool = False
) -> LLMProvider:
    """The platform's own model account, as a platform administrator configured it."""
    if light or voice:
        model = (VOICE_MODELS if voice else LIGHT_MODELS)[settings.llm_provider]
    else:
        model = settings.llm_model
    return _provider(settings.llm_provider, settings.platform_llm_key, model, voice=voice)


def llm_from_connection(
    provider: str,
    api_key: SecretStr,
    config: dict[str, Any],
    *,
    light: bool = False,
    voice: bool = False,
) -> LLMProvider:
    """One tenant's own model connection."""
    if light or voice:
        model = (VOICE_MODELS if voice else LIGHT_MODELS)[provider]
    else:
        model = str(config["model"])
    return _provider(provider, api_key, model, config.get("thinking_level", "low"), voice=voice)
