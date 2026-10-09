import asyncio
from urllib.parse import unquote

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Tracer

from app.core.settings import Settings

TRACE_SAMPLE_RATIO = 0.1
METRIC_EXPORT_INTERVAL_MILLIS = 30_000


class Telemetry:
    """Application-owned providers avoid global singletons across tests and app instances."""

    def __init__(self, settings: Settings, service: str = "ai-frontdesk") -> None:
        resource = Resource.create(
            {
                "service.name": service,
                "deployment.environment.name": settings.environment,
            }
        )
        self.traces = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(TRACE_SAMPLE_RATIO)),
            shutdown_on_exit=False,
        )
        readers: list[PeriodicExportingMetricReader] = []
        if settings.otel_exporter_otlp_endpoint:
            endpoint = settings.otel_exporter_otlp_endpoint
            headers = {
                key.strip(): unquote(value.strip())
                for entry in settings.otel_exporter_otlp_headers.get_secret_value().split(",")
                if entry.strip()
                for key, _, value in [entry.partition("=")]
            }
            self.traces.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers, timeout=5)
                )
            )
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=f"{endpoint}/v1/metrics", headers=headers, timeout=5
                    ),
                    export_interval_millis=METRIC_EXPORT_INTERVAL_MILLIS,
                )
            )
        self.metrics = MeterProvider(
            resource=resource, metric_readers=readers, shutdown_on_exit=False
        )
        self.tracer: Tracer = self.traces.get_tracer("app.http")
        self.meter: Meter = self.metrics.get_meter("app.http")
        self.request_duration = self.meter.create_histogram(
            "http.server.request.duration", unit="s"
        )

    async def close(self) -> None:
        # Exporters are synchronous; flushing them must not block the event loop.
        try:
            await asyncio.to_thread(self.metrics.shutdown, timeout_millis=5000)
        finally:
            await asyncio.to_thread(self.traces.shutdown)
