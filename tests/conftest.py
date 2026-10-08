import pytest
from pydantic import SecretStr

from app.config.settings import Settings


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
