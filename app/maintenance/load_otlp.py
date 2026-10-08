"""Ephemeral OTLP metric receiver and cumulative-delta summarizer for load tests."""

import asyncio
import math
import time
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)


@dataclass(frozen=True)
class HistogramValue:
    bounds: tuple[float, ...]
    buckets: tuple[int, ...]


@dataclass(frozen=True)
class MetricSnapshot:
    histograms: dict[str, HistogramValue]
    counters: dict[str, float]


def _percentile(
    buckets: tuple[int, ...], bounds: tuple[float, ...], fraction: float
) -> float | None:
    total = sum(buckets)
    if total == 0:
        return None
    target = math.ceil(total * fraction)
    cumulative = 0
    for count, upper in zip(buckets, bounds, strict=False):
        cumulative += count
        if cumulative >= target:
            return upper
    return None  # The requested percentile exceeded the largest explicit bucket.


class MetricStore:
    def __init__(self) -> None:
        self.received = asyncio.Event()
        self.histograms: dict[str, HistogramValue] = {}
        self.counters: dict[str, float] = {}
        self.pool_samples: list[tuple[float, float]] = []
        self.last_received_at: float | None = None

    def ingest(self, payload: bytes) -> None:
        request = ExportMetricsServiceRequest()
        request.ParseFromString(payload)
        histograms: dict[str, HistogramValue] = {}
        counters: dict[str, float] = {}
        for resource in request.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.HasField("histogram"):
                        for point in metric.histogram.data_points:
                            bounds = tuple(float(value) for value in point.explicit_bounds)
                            buckets = tuple(int(value) for value in point.bucket_counts)
                            previous = histograms.get(metric.name)
                            if previous is not None and previous.bounds == bounds:
                                buckets = tuple(
                                    left + right
                                    for left, right in zip(previous.buckets, buckets, strict=True)
                                )
                            histograms[metric.name] = HistogramValue(bounds, buckets)
                    if metric.HasField("sum"):
                        counters[metric.name] = sum(
                            float(point.as_int if point.HasField("as_int") else point.as_double)
                            for point in metric.sum.data_points
                        )
                    if metric.name == "db.pool.checked_out" and metric.HasField("gauge"):
                        for point in metric.gauge.data_points:
                            value = float(
                                point.as_int if point.HasField("as_int") else point.as_double
                            )
                            self.pool_samples.append((time.monotonic(), value))
        self.histograms.update(histograms)
        self.counters.update(counters)
        self.last_received_at = time.monotonic()
        self.received.set()

    def snapshot(self) -> MetricSnapshot:
        return MetricSnapshot(dict(self.histograms), dict(self.counters))

    def summarize(
        self, before: MetricSnapshot, after: MetricSnapshot, start: float, end: float
    ) -> dict[str, float | int | None]:
        result: dict[str, float | int | None] = {}
        for label, name in (
            ("stt", "voice.stt.finalization.latency"),
            ("llm", "voice.llm.latency"),
            ("tts", "voice.tts.pipeline.first_audio.latency"),
        ):
            current = after.histograms.get(name)
            previous = before.histograms.get(name)
            if current is None:
                result[f"{label}_samples"] = 0
                result[f"{label}_p50_ms_upper_bound"] = None
                result[f"{label}_p95_ms_upper_bound"] = None
                continue
            buckets = (
                tuple(
                    max(0, new - old)
                    for new, old in zip(current.buckets, previous.buckets, strict=True)
                )
                if previous is not None and previous.bounds == current.bounds
                else current.buckets
            )
            result[f"{label}_samples"] = sum(buckets)
            result[f"{label}_p50_ms_upper_bound"] = _percentile(buckets, current.bounds, 0.5)
            result[f"{label}_p95_ms_upper_bound"] = _percentile(buckets, current.bounds, 0.95)
        name = "voice.provider.fallback.total"
        result["provider_fallback_count"] = max(
            0, int(after.counters.get(name, 0) - before.counters.get(name, 0))
        )
        pool = [value for stamp, value in self.pool_samples if start <= stamp <= end]
        result["db_pool_checked_out_peak"] = max(pool, default=None)
        return result


def collector_app(store: MetricStore) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/v1/metrics")
    async def receive_metrics(request: Request) -> Response:
        body = await request.body()
        if len(body) > 2_000_000:
            raise HTTPException(413)
        try:
            store.ingest(body)
        except Exception:
            raise HTTPException(400) from None
        return Response(status_code=200)

    @app.post("/v1/traces")
    async def discard_traces(request: Request) -> Response:
        if len(await request.body()) > 2_000_000:
            raise HTTPException(413)
        return Response(status_code=200)

    return app


async def start_collector(
    store: MetricStore, host: str, port: int
) -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = uvicorn.Server(
        uvicorn.Config(
            collector_app(store), host=host, port=port, log_level="error", access_log=False
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            return server, task
        if task.done():
            break
        await asyncio.sleep(0.05)
    server.should_exit = True
    await task
    raise RuntimeError("OTLP metric receiver failed to start")
