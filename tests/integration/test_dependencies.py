import os

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.cache.redis import RedisClient
from app.config.settings import Settings
from app.database.session import Database
from app.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_INTEGRATION") != "true"
        and os.environ.get("RUN_POSTGRES_INTEGRATION") != "true",
        reason="set RUN_INTEGRATION=true (full) or RUN_POSTGRES_INTEGRATION=true (database only)",
    ),
]


def test_alembic_upgrade_and_metadata_match() -> None:
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.check(config)


async def test_real_database_connections_and_sessions() -> None:
    settings = Settings.from_environment()
    database = Database(settings)
    try:
        await database.ping()
        async with database.session() as first, database.session() as second:
            assert first is not second
            assert await first.scalar(text("SELECT current_database()")) is not None
            assert await first.scalar(text("SHOW TIMEZONE")) == "UTC"
        # A temporary table stays on the same pooled connection; rollback undoes its DDL.
        async with database.session() as session:
            await session.execute(
                text("CREATE TEMP TABLE foundation_rollback_probe (value integer)")
            )
        async with database.session() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('foundation_rollback_probe')"))
                is None
            )
    finally:
        await database.close()


@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "true", reason="requires real Redis too")
async def test_real_redis_and_health() -> None:
    settings = Settings.from_environment()
    redis = RedisClient(settings)
    try:
        await redis.ping()
    finally:
        await redis.close()
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["checks"] == {"postgresql": "ok", "redis": "ok"}
