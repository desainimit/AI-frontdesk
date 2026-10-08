import os

import pytest
from pydantic import SecretStr

from app.config.settings import Settings

# A developer's .env may hold real provider keys. No test may ever pick them up: a
# test that built a real client would place billed calls. The process environment
# overrides .env, so these are blanked before any Settings object is created.
for _secret in (
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "AZURE_SPEECH_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
):
    os.environ[_secret] = ""
os.environ["LLM_PROVIDER"] = "mock"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "local")
    return Settings.from_environment(
        env_file=None,
        database_url=SecretStr("postgresql+asyncpg://test:test@localhost:5432/frontdesk_test"),
        redis_url=SecretStr("redis://localhost:6379/15"),
        dependency_timeout_seconds=0.02,
    )
