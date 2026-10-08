from app.agents.providers.base import LLMProvider
from app.agents.providers.mock import MockLLMProvider
from app.config.settings import Settings


def build_llm_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockLLMProvider()
    from app.agents.providers.openai import OpenAILLMProvider

    return OpenAILLMProvider(
        settings.openai_api_key,
        settings.llm_model,
        settings.llm_timeout_seconds,
        settings.llm_max_output_tokens,
    )
