import asyncio
import json
import socket
import time
import wave
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from websockets.asyncio.client import ClientConnection

from app.config.settings import Environment, Settings
from app.database.session import Database
from app.main import create_app
from app.maintenance import call_load
from app.maintenance.call_load import PreparedCall, audio_frames, percentile, run_tier
from app.maintenance.load_otlp import MetricStore, collector_app, start_collector
from app.maintenance.load_resources import ResourceSamples
from app.observability.tracing import Telemetry
from app.voice.metrics import VoiceMetrics
from app.voice.twilio import signature_valid
from tests.fakes import FakeDependency
from tests.test_telephony import voice_settings


class EchoSocket:
    def __init__(self) -> None:
        self.outbound: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send(self, data: str) -> None:
        event = json.loads(data)
        self.sent.append(event)
        if event["event"] == "media":
            await self.outbound.put(json.dumps({"event": "media", "media": {"payload": "AAAA"}}))
        if event["event"] == "stop":
            await self.outbound.put(None)

    async def close(self) -> None:
        self.closed = True

    async def __aiter__(self):
        while (item := await self.outbound.get()) is not None:
            yield item


def test_audio_fixture_validation_and_percentiles(tmp_path: Path) -> None:
    assert len(audio_frames(None, echo=True)) == 1
    with pytest.raises(ValueError, match="require"):
        audio_frames(None, echo=False)
    fixture = tmp_path / "speech.wav"
    with wave.open(str(fixture), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(b"\x00\x00" * 320)
    assert len(audio_frames(fixture, echo=False)) == 2
    assert percentile([1, 2, 3, 4], 0.95) == 4


async def test_prepare_signs_incoming_and_binds_media_socket(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    config = voice_settings(settings)
    media_path = (
        "/webhooks/v1/twilio/00000000-0000-0000-0000-000000000001/"
        "00000000-0000-0000-0000-000000000002/media"
    )
    socket = EchoSocket()

    async def fake_connect(url: str, **kwargs: Any) -> ClientConnection:
        assert url == "ws://127.0.0.1:8000" + media_path
        assert signature_valid(
            config, media_path, kwargs["additional_headers"]["X-Twilio-Signature"], websocket=True
        )
        return cast(ClientConnection, socket)

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(httpx.QueryParams(request.content.decode()))
        assert signature_valid(
            config, request.url.path, request.headers["X-Twilio-Signature"], params
        )
        xml = (
            '<Response><Connect><Stream url="wss://voice.example.test'
            + media_path
            + '"><Parameter name="token" value="bound-token" /></Stream></Connect></Response>'
        )
        return httpx.Response(200, text=xml)

    monkeypatch.setattr(call_load, "connect", fake_connect)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8000"
    ) as client:
        prepared = await call_load.prepare(client, "http://127.0.0.1:8000", config, "+15551234567")
    assert prepared.socket is socket
    assert [event["event"] for event in socket.sent] == ["connected", "start"]
    assert socket.sent[1]["start"]["customParameters"]["token"] == "bound-token"


async def test_full_protocol_driver_runs_each_concurrency_level(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    sockets: list[EchoSocket] = []

    async def fake_prepare(*_args: Any) -> PreparedCall:
        socket = EchoSocket()
        sockets.append(socket)
        return PreparedCall(
            cast(ClientConnection, socket), "MZ" + "0" * 32, "CA" + "0" * 32, "AC" + "0" * 32
        )

    async def fake_monitor(
        _settings: Settings, _container: str, stop: asyncio.Event
    ) -> ResourceSamples:
        await stop.wait()
        return ResourceSamples(cpu_percent=[12.5], memory_bytes=[1024])

    monkeypatch.setattr(call_load, "prepare", fake_prepare)
    monkeypatch.setattr(call_load, "monitor", fake_monitor)
    async with httpx.AsyncClient() as client:
        for level in (10, 50, 100):
            result = await run_tier(
                client,
                "http://localhost:8000",
                settings,
                "+15551234567",
                [call_load.SILENCE],
                level,
                0.03,
            )
            assert result["concurrency_connected"] == level
            assert result["successes"] == level
            assert result["errors"] == 0
    assert len(sockets) == 160 and all(socket.closed for socket in sockets)


def _export(count: int, fallback: int) -> bytes:
    request = ExportMetricsServiceRequest()
    scope = request.resource_metrics.add().scope_metrics.add()
    for name in (
        "voice.stt.finalization.latency",
        "voice.llm.latency",
        "voice.tts.pipeline.first_audio.latency",
    ):
        metric = scope.metrics.add()
        metric.name = name
        point = metric.histogram.data_points.add()
        point.explicit_bounds.extend([10, 100])
        point.bucket_counts.extend([count, count, 0])
        point.count = count * 2
    metric = scope.metrics.add()
    metric.name = "voice.provider.fallback.total"
    metric.sum.data_points.add().as_int = fallback
    metric = scope.metrics.add()
    metric.name = "db.pool.checked_out"
    metric.gauge.data_points.add().as_int = 4
    return request.SerializeToString()


async def test_otlp_receiver_reports_per_tier_deltas() -> None:
    store = MetricStore()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=collector_app(store)), base_url="http://localhost"
    ) as client:
        assert (await client.post("/v1/metrics", content=_export(2, 1))).status_code == 200
        before = store.snapshot()
        start = time.monotonic()
        assert (await client.post("/v1/metrics", content=_export(5, 3))).status_code == 200
        result = store.summarize(before, store.snapshot(), start, time.monotonic())
        assert result["llm_samples"] == 6
        assert result["llm_p95_ms_upper_bound"] == 100
        assert result["provider_fallback_count"] == 2
        assert result["db_pool_checked_out_peak"] == 4


async def test_app_metric_export_reaches_load_receiver(settings: Settings) -> None:
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    store = MetricStore()
    server, task = await start_collector(store, "127.0.0.1", port)
    config = settings.model_copy(
        update={
            "otel_enabled": True,
            "otel_exporter_otlp_endpoint": f"http://127.0.0.1:{port}",
            "otel_metric_export_interval_seconds": 1,
        }
    )
    telemetry = Telemetry(config)
    try:
        VoiceMetrics(telemetry).record_llm_latency(12, "mock")
        await asyncio.wait_for(store.received.wait(), 5)
        assert sum(store.histograms["voice.llm.latency"].buckets) == 1
    finally:
        await telemetry.close()
        server.should_exit = True
        await task


async def test_load_refuses_production_before_network(settings: Settings) -> None:
    args = type("Args", (), {"base_url": "http://127.0.0.1:8000", "allow_remote": False})()
    with pytest.raises(ValueError, match="production"):
        await call_load.run(
            cast(Any, args), settings.model_copy(update={"environment": Environment.PRODUCTION})
        )


def test_real_pool_gauge_registers_during_app_lifespan(settings: Settings) -> None:
    app = create_app(settings, database_factory=Database, redis_factory=lambda _: FakeDependency())
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
