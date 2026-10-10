from typing import Any

from pydantic import SecretStr

from app.agents.providers.base import LLMProvider
from app.core.settings import Settings

TIMEOUT_SECONDS = 10
# Thinking tokens count against this budget, so it leaves room for the reply.
MAX_OUTPUT_TOKENS = 2048


def _provider(
    settings: Settings, name: str, api_key: SecretStr, model: str, reasoning: Any = "low"
) -> LLMProvider:
    if name == "gemini":
        from app.agents.providers.gemini import GeminiLLMProvider

        return GeminiLLMProvider(
            api_key,
            model,
            TIMEOUT_SECONDS,
            MAX_OUTPUT_TOKENS,
            thinking_level=reasoning,
        )
    if name == "openrouter":
        from app.agents.providers.openrouter import OpenRouterLLMProvider

        return OpenRouterLLMProvider(
            api_key,
            model,
            TIMEOUT_SECONDS,
            MAX_OUTPUT_TOKENS,
            reasoning_effort=reasoning,
        )
    raise ValueError("Unsupported LLM provider")


def build_llm_provider(settings: Settings) -> LLMProvider:
    """The platform's own model account, as a platform administrator configured it."""
    return _provider(settings, settings.llm_provider, settings.platform_llm_key, settings.llm_model)


def llm_from_connection(
    settings: Settings, provider: str, api_key: SecretStr, config: dict[str, Any]
) -> LLMProvider:
    """One tenant's own model connection."""
    return _provider(
        settings, provider, api_key, str(config["model"]), config.get("thinking_level", "low")
    )
