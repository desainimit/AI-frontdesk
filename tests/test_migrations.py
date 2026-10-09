import os
import subprocess
import sys

from app.core.settings import Settings


def test_offline_migration_sql(settings: Settings) -> None:
    environment = {
        **os.environ,
        "DATABASE_URL": settings.database_url.get_secret_value(),
        "REDIS_URL": settings.redis_url.get_secret_value(),
        "MIGRATION_DATABASE_URL": settings.database_url.get_secret_value(),
    }
    for arguments, expected in [
        (["upgrade", "head", "--sql"], "INSERT INTO alembic_version"),
        (["downgrade", "0001_baseline:base", "--sql"], "DELETE FROM alembic_version"),
    ]:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        assert expected in result.stdout
        assert "test:test" not in result.stdout
