from typing import Any

from pydantic import SecretStr

from app.ai.llm.base import LLMProvider
from app.core.settings import Settings

TIMEOUT_SECONDS = 10
# Thinking tokens count against this budget, so it leaves room for the reply.
MAX_OUTPUT_TOKENS = 2048
# The quicker, cheaper model: the safety check, summaries, and one last attempt at a
# reply when the main model keeps failing.
LIGHT_MODELS = {"gemini": "gemini-3.5-flash-lite", "openrouter": "google/gemini-3.5-flash-lite"}


# On a call every second of thinking is a second of silence, so voice turns think as
# little as the model allows and go to the quickest endpoint.
VOICE_REASONING = "minimal"


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
    model = LIGHT_MODELS[settings.llm_provider] if light else settings.llm_model
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
    model = LIGHT_MODELS[provider] if light else str(config["model"])
    return _provider(provider, api_key, model, config.get("thinking_level", "low"), voice=voice)
