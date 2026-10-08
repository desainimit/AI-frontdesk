from typing import cast

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.config.settings import Settings
from app.main import create_app
from app.observability.tracing import Telemetry
from tests.fakes import FakeDependency


def test_trace_parent_propagation_and_sensitive_data_exclusion(settings: Settings) -> None:
    settings.otel_trace_sample_ratio = 1
    app = create_app(
        settings,
        database_factory=lambda _: FakeDependency(),
        redis_factory=lambda _: FakeDependency(),
    )

    @app.get("/records/{record_id}")
    async def fail(record_id: str) -> None:
        raise RuntimeError(f"private medical record {record_id}")

    exporter = InMemorySpanExporter()
    with TestClient(app, raise_server_exceptions=False) as client:
        telemetry = cast(Telemetry, app.state.telemetry)
        telemetry.traces.add_span_processor(SimpleSpanProcessor(exporter))
        response = client.get(
            "/records/sensitive-id?password=secret",
            headers={"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"},
        )
        assert response.status_code == 500
        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert span.context is not None
        assert span.context.trace_id == int("0123456789abcdef0123456789abcdef", 16)
        assert span.parent is not None
        assert span.parent.span_id == int("0123456789abcdef", 16)
        assert span.status.status_code == StatusCode.ERROR
        assert span.name == "GET /records/{record_id}"
        assert span.events == ()
        serialized = str(span.attributes)
        assert all(word not in serialized for word in ("private", "sensitive-id", "secret"))


async def test_export_enabled_initializes_and_closes(settings: Settings) -> None:
    # No spans/measurements: this verifies real SDK setup and shutdown without external IO.
    settings.otel_enabled = True
    settings.otel_exporter_otlp_endpoint = "http://127.0.0.1:4318"
    telemetry = Telemetry(settings)
    await telemetry.close()
