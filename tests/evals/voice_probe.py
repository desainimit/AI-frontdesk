"""How quickly each model starts answering on the real call prompt.

    EVAL_PROVIDER=openrouter EVAL_API_KEY=... \
    EVAL_MODELS=google/gemini-3.5-flash-lite,anthropic/claude-haiku-5.5 \
    python -m tests.evals.voice_probe

Billed: three short replies per language per model (well under a cent per model), set
up exactly as on a call (least thinking, quickest endpoint). Needs no database.
"""

import asyncio
import os
import statistics
import sys
from time import perf_counter
from unittest.mock import MagicMock
from uuid import uuid4

from pydantic import SecretStr

from app.ai.llm.base import ChatRequest, Completed, LLMProviderError, TextDelta, UserTurn
from app.ai.llm.factory import provider_for
from app.ai.llm.models import profile
from app.ai.prompts import prompt_parts
from app.conversation.locales import LanguagePolicy
from app.conversation.manager import ConversationStateManager
from app.settings.defaults import default_settings

LINES = {
    "en": "Hi, can I see a doctor tomorrow morning?",
    "hi": "नमस्ते, क्या कल सुबह डॉक्टर से मिल सकते हैं?",
    "gu": "નમસ્તે, કાલે સવારે ડૉક્ટરને મળી શકાય?",
}
TRIES = 3


async def probe(model: str, key: str) -> None:
    configuration = default_settings("Lotus Clinic")
    session = MagicMock(configuration=configuration, channel="voice", contact=None, directory=None)
    for language, line in LINES.items():
        # A clinic that offers just this language, so the call is held in it.
        state = ConversationStateManager.create_initial_state(
            uuid4(),
            uuid4(),
            "CA" + "0" * 32,
            default_language=language,
            language_policy=LanguagePolicy(default_language=language, enabled_languages=[language]),
        )
        system, context = prompt_parts(session, state)
        first: list[int] = []
        cost = 0.0
        for _ in range(TRIES):
            provider = provider_for(model, SecretStr(key), voice=True)
            started = perf_counter()
            try:
                async for event in provider.stream(
                    ChatRequest(system, (UserTurn(line),), (), context, uuid4().hex)
                ):
                    if isinstance(event, TextDelta) and started > 0:
                        first.append(int((perf_counter() - started) * 1000))
                        started = 0.0
                    elif isinstance(event, Completed):
                        cost += event.usage.cost_usd
            except LLMProviderError as error:
                print(f"{model:36s} {language}  failed: {error.code}")
            finally:
                await provider.close()
        median = int(statistics.median(first)) if first else 0
        print(f"{model:36s} {language}  first words {median:>5} ms (median)  ${cost:.5f}")


async def main() -> int:
    # Replies in Hindi or Gujarati must print on a Windows console too.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    provider, key = os.environ.get("EVAL_PROVIDER", ""), os.environ.get("EVAL_API_KEY", "")
    models = [m.strip() for m in os.environ.get("EVAL_MODELS", "").split(",") if m.strip()]
    if provider not in {"openrouter", "gemini"} or not models or not key:
        print("Set EVAL_PROVIDER, EVAL_MODELS and EVAL_API_KEY.")
        return 2
    for model in models:
        found = profile(model)
        if found is None or found.provider != provider or "voice" not in found.jobs:
            print(f"{model} is not a catalog model for calls on {provider}.")
            return 2
    for model in models:
        await probe(model, key)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
