import pytest
from pydantic import SecretStr, ValidationError

from app.core.settings import Settings


def test_secrets_masked_and_urls_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:private@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://:private@localhost:6379/0")
    settings = Settings.from_environment(env_file=None)
    assert "private" not in repr(settings)
    assert "private" not in settings.model_dump_json()
    with pytest.raises(ValidationError) as exc:
        Settings.from_environment(env_file=None, database_url=SecretStr("sqlite://private"))
    assert "private" not in str(exc.value)
    with pytest.raises(ValidationError):
        Settings.from_environment(env_file=None, redis_url=SecretStr("https://localhost"))


def test_required_settings_have_no_credential_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings.from_environment(env_file=None)
