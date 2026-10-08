import pytest
from pydantic import SecretStr

from app.config.settings import Settings
from app.database.provision import (
    ProvisioningConfigurationError,
    main,
    validate_role_configuration,
)


def test_same_login_is_explained_without_exposing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "postgresql+asyncpg://postgres:private-password@localhost/frontdesk"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    with pytest.raises(SystemExit) as error:
        main()
    message = str(error.value)
    assert "must use different PostgreSQL users" in message
    assert "DATABASE_URL" in message and "MIGRATION_DATABASE_URL" in message
    assert "private-password" not in message
    assert url not in message


def test_distinct_runtime_and_migration_logins_are_valid(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "database_url": SecretStr(
                "postgresql+asyncpg://frontdesk_app:private@localhost/frontdesk"
            ),
            "migration_database_url": SecretStr(
                "postgresql+asyncpg://postgres:private@localhost/frontdesk"
            ),
        }
    )
    assert validate_role_configuration(configured) == "frontdesk_app"


@pytest.mark.parametrize(
    "migration,expected",
    [
        (None, "Set MIGRATION_DATABASE_URL"),
        (SecretStr("postgresql+asyncpg://owner:private@localhost/another_db"), "same database"),
    ],
)
def test_missing_or_mismatched_migration_config(
    settings: Settings,
    migration: SecretStr | None,
    expected: str,
) -> None:
    configured = settings.model_copy(update={"migration_database_url": migration})
    with pytest.raises(ProvisioningConfigurationError, match=expected):
        validate_role_configuration(configured)
