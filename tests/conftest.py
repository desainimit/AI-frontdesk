import os

import pytest
from pydantic import SecretStr

from app.core.settings import Settings

# A developer's .env may hold real provider keys, a real mail server and public
# addresses. No test may ever pick them up: a test that built a real client would
# place billed calls or send real email. The process environment overrides .env, so
# these are blanked before any Settings object is created; a test that needs one
# sets its own value.
for _secret in (
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "AZURE_SPEECH_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "SMTP_HOST",
    "SMTP_SENDER",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SECRET_ENCRYPTION_KEYS",
    "SECRET_ENCRYPTION_ACTIVE_KEY",
    "OTP_HMAC_KEY",
    "STREAM_TOKEN_KEY",
    "TENANT_AUTH_PUBLIC_ORIGIN",
    "TELEPHONY_PUBLIC_BASE_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
):
    os.environ[_secret] = ""
# The semantic safety check is one more model request per turn; tests of it enable it.
os.environ["SAFETY_SEMANTIC_ENABLED"] = "false"


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ENVIRONMENT", "local")
    return Settings.from_environment(
        env_file=None,
        database_url=SecretStr("postgresql+asyncpg://test:test@localhost:5432/frontdesk_test"),
        redis_url=SecretStr("redis://localhost:6379/15"),
        dependency_timeout_seconds=0.02,
    )
