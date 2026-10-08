from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.errors import ConflictError
from app.config.settings import Settings
from app.main import create_app
from tests.fakes import FakeDependency


def make_error_app(settings: Settings) -> FastAPI:
    app = create_app(
        settings,
        database_factory=lambda _: FakeDependency(),
        redis_factory=lambda _: FakeDependency(),
        rate_limits=False,
    )

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("password=secret medical information")

    @app.get("/conflict")
    async def conflict() -> None:
        raise ConflictError("private internal detail")

    @app.get("/validate")
    async def validate(count: int) -> int:
        return count

    return app


def test_errors_do_not_expose_internal_or_input_values(settings: Settings) -> None:
    with TestClient(make_error_app(settings), raise_server_exceptions=False) as client:
        for path, status, code in [
            ("/explode", 500, "internal_error"),
            ("/conflict", 409, "conflict"),
            ("/validate?count=password-secret", 422, "validation_error"),
            ("/missing", 404, "http_error"),
        ]:
            response = client.get(path)
            assert response.status_code == status
            assert response.json()["error"]["code"] == code
            assert response.json()["request_id"] == response.headers["x-request-id"]
            assert all(word not in response.text for word in ["secret", "private", "medical"])
