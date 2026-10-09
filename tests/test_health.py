from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app
from tests.fakes import FakeDependency


def test_liveness_never_contacts_dependencies(settings: Settings) -> None:
    database = FakeDependency(failing=True)
    redis = FakeDependency(failing=True)
    app = create_app(settings, database_factory=lambda _: database, redis_factory=lambda _: redis)
    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}
        assert response.headers["cache-control"] == "no-store"
        assert UUID(response.headers["x-request-id"])
        assert database.pings == redis.pings == 0


@pytest.mark.parametrize(
    "database_fails,redis_fails", [(False, False), (True, False), (False, True), (True, True)]
)
def test_readiness_reports_each_dependency(
    settings: Settings, database_fails: bool, redis_fails: bool
) -> None:
    database = FakeDependency(failing=database_fails)
    redis = FakeDependency(failing=redis_fails)
    app = create_app(settings, database_factory=lambda _: database, redis_factory=lambda _: redis)
    with TestClient(app) as client:
        response = client.get("/health/ready")
        healthy = not (database_fails or redis_fails)
        assert response.status_code == (200 if healthy else 503)
        assert response.json() == {
            "status": "ready" if healthy else "not_ready",
            "checks": {
                "postgresql": "unavailable" if database_fails else "ok",
                "redis": "unavailable" if redis_fails else "ok",
            },
        }
        assert "sensitive-password" not in response.text
        assert database.pings == redis.pings == 1


def test_readiness_timeout_cancels_probe(settings: Settings) -> None:
    database = FakeDependency(hanging=True)
    redis = FakeDependency()
    app = create_app(settings, database_factory=lambda _: database, redis_factory=lambda _: redis)
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert database.cancelled
        assert redis.pings == 1


def test_recovery_and_request_ids(settings: Settings) -> None:
    database = FakeDependency(failing=True)
    redis = FakeDependency()
    app = create_app(settings, database_factory=lambda _: database, redis_factory=lambda _: redis)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 503
        database.failing = False
        request_id = str(uuid4())
        response = client.get("/health/ready", headers={"X-Request-ID": request_id})
        assert response.status_code == 200
        assert response.headers["x-request-id"] == request_id
        invalid = client.get("/health/live", headers={"X-Request-ID": "caller-controlled-secret"})
        assert UUID(invalid.headers["x-request-id"])
