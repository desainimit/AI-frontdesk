from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.core.telemetry import Telemetry
from app.main import create_app
from tests.fakes import FakeDependency


def test_resources_closed_on_shutdown(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[Telemetry] = []
    original_close = Telemetry.close

    async def close_telemetry(self: Telemetry) -> None:
        closed.append(self)
        await original_close(self)

    monkeypatch.setattr(Telemetry, "close", close_telemetry)
    database = FakeDependency()
    redis = FakeDependency()
    app = create_app(settings, database_factory=lambda _: database, redis_factory=lambda _: redis)
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert not database.closed and not redis.closed
    assert database.closed and redis.closed
    assert closed == [cast(Telemetry, app.state.telemetry)]


def test_partial_startup_closes_created_resources(settings: Settings) -> None:
    database = FakeDependency()

    def broken_redis(_: Settings) -> FakeDependency:
        raise RuntimeError("constructor failed")

    app = create_app(settings, database_factory=lambda _: database, redis_factory=broken_redis)
    with pytest.raises(RuntimeError, match="constructor failed"), TestClient(app):
        pass
    assert database.closed


def test_cleanup_continues_when_one_resource_close_fails(settings: Settings) -> None:
    class BrokenClose(FakeDependency):
        async def close(self) -> None:
            raise RuntimeError("close failed")

    database = FakeDependency()
    app = create_app(
        settings, database_factory=lambda _: database, redis_factory=lambda _: BrokenClose()
    )
    with pytest.raises(RuntimeError, match="close failed"), TestClient(app):
        pass
    assert database.closed
