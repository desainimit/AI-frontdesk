"""Drive signed, synthetic Twilio media sessions against a non-production deployment."""

import argparse
import asyncio
import audioop
import base64
import json
import math
import statistics
import time
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree

import httpx
from twilio.request_validator import RequestValidator  # pyright: ignore[reportMissingTypeStubs]
from websockets.asyncio.client import ClientConnection, connect

from app.config.settings import Environment, Settings
from app.maintenance.load_otlp import MetricStore, start_collector
from app.maintenance.load_resources import monitor

FRAME_BYTES = 160  # 20 ms of 8 kHz, 8-bit mu-law audio.
SILENCE = base64.b64encode(b"\xff" * FRAME_BYTES).decode("ascii")


@dataclass(frozen=True)
class CallResult:
    success: bool
    first_audio_ms: float | None = None
    post_utterance_audio_ms: float | None = None
    error_stage: str | None = None


@dataclass
class PreparedCall:
    socket: ClientConnection
    stream_sid: str
    call_sid: str
    account_sid: str


def audio_frames(path: Path | None, *, echo: bool) -> list[str]:
    if path is None:
        if not echo:
            raise ValueError("Pipeline load tests require an 8 kHz mono PCM16 speech WAV")
        return [SILENCE]
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 8000):
            raise ValueError("Speech WAV must be mono 8 kHz 16-bit PCM")
        if source.getnframes() < 160 or source.getnframes() > 8000 * 15:
            raise ValueError("Speech WAV must contain between 20 ms and 15 s of audio")
        data = source.readframes(source.getnframes())
    frames = [
        base64.b64encode(audioop.lin2ulaw(data[index : index + 320], 2)).decode("ascii")
        for index in range(0, len(data), 320)
    ]
    return frames


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def failure_stage(error: BaseException) -> str:
    if isinstance(error, RuntimeError):
        label = str(error)
        if label in {"missing_stream", "invalid_stream_origin", "invalid_stream_binding"}:
            return label
        if label.startswith("incoming_http_") and label.removeprefix("incoming_http_").isdigit():
            return label
    return type(error).__name__


def _signature(settings: Settings, path: str, params: dict[str, str]) -> str:
    validator = RequestValidator(settings.twilio_auth_token.get_secret_value())
    return validator.compute_signature(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        settings.telephony_public_base_url + path, params
    )


async def prepare(
    client: httpx.AsyncClient, base_url: str, settings: Settings, number: str
) -> PreparedCall:
    path = "/webhooks/v1/twilio/incoming"
    call_sid = "CA" + uuid4().hex
    params = {"AccountSid": settings.twilio_account_sid, "CallSid": call_sid, "To": number}
    response = await client.post(
        path, data=params, headers={"X-Twilio-Signature": _signature(settings, path, params)}
    )
    if response.status_code != 200:
        raise RuntimeError(f"incoming_http_{response.status_code}")
    stream = ElementTree.fromstring(response.text).find("Connect/Stream")
    if stream is None:
        raise RuntimeError("missing_stream")
    public_url = stream.attrib.get("url", "")
    public = urlsplit(public_url)
    expected = urlsplit(settings.telephony_public_base_url)
    if public.scheme != "wss" or public.netloc != expected.netloc:
        raise RuntimeError("invalid_stream_origin")
    token = next(
        (
            node.attrib.get("value", "")
            for node in stream.findall("Parameter")
            if node.attrib.get("name") == "token"
        ),
        "",
    )
    if not token or not public.path.startswith("/webhooks/v1/twilio/"):
        raise RuntimeError("invalid_stream_binding")
    local = urlsplit(base_url)
    ws_url = f"{'wss' if local.scheme == 'https' else 'ws'}://{local.netloc}{public.path}"
    socket = await connect(
        ws_url,
        additional_headers={"X-Twilio-Signature": _signature(settings, public.path, {})},
        max_size=16384,
        open_timeout=15,
        close_timeout=5,
    )
    stream_sid = "MZ" + uuid4().hex
    try:
        await socket.send(
            json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        )
        await socket.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "streamSid": stream_sid,
                    "start": {
                        "streamSid": stream_sid,
                        "accountSid": settings.twilio_account_sid,
                        "callSid": call_sid,
                        "tracks": ["inbound"],
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": 8000,
                            "channels": 1,
                        },
                        "customParameters": {"token": token},
                    },
                }
            )
        )
    except Exception:
        await socket.close()
        raise
    return PreparedCall(socket, stream_sid, call_sid, settings.twilio_account_sid)


async def drive(call: PreparedCall, frames: list[str], hold_seconds: float) -> CallResult:
    first_sent: float | None = None
    utterance_end: float | None = None
    first_received: float | None = None
    sequence = 1

    async def send_audio() -> None:
        nonlocal first_sent, utterance_end, sequence
        deadline = time.monotonic() + hold_seconds
        next_frame = time.monotonic()
        index = 0
        while time.monotonic() < deadline:
            sequence += 1
            payload = frames[index] if index < len(frames) else SILENCE
            await call.socket.send(
                json.dumps(
                    {
                        "event": "media",
                        "sequenceNumber": str(sequence),
                        "streamSid": call.stream_sid,
                        "media": {"track": "inbound", "payload": payload},
                    }
                )
            )
            now = time.monotonic()
            if first_sent is None:
                first_sent = now
            if index == len(frames) - 1:
                utterance_end = now
            index += 1
            next_frame += 0.02
            await asyncio.sleep(max(0, next_frame - time.monotonic()))
        sequence += 1
        await call.socket.send(
            json.dumps(
                {
                    "event": "stop",
                    "sequenceNumber": str(sequence),
                    "streamSid": call.stream_sid,
                    "stop": {"accountSid": call.account_sid, "callSid": call.call_sid},
                }
            )
        )

    async def receive_audio() -> None:
        nonlocal first_received
        async for raw in call.socket:
            event = json.loads(raw)
            if event.get("event") == "media" and first_received is None:
                first_received = time.monotonic()

    try:
        async with asyncio.timeout(hold_seconds + 20):
            async with asyncio.TaskGroup() as group:
                group.create_task(send_audio())
                group.create_task(receive_audio())
        if first_sent is None or first_received is None:
            return CallResult(False, error_stage="no_response_audio")
        first_ms = max(0.0, (first_received - first_sent) * 1000)
        after_end = (
            max(0.0, (first_received - utterance_end) * 1000)
            if utterance_end is not None and first_received >= utterance_end
            else None
        )
        return CallResult(True, first_ms, after_end)
    except Exception:
        return CallResult(False, error_stage="media_session_failed")
    finally:
        await call.socket.close()


async def run_tier(
    client: httpx.AsyncClient,
    base_url: str,
    settings: Settings,
    number: str,
    frames: list[str],
    concurrent: int,
    hold_seconds: float,
    container: str = "ai-frontdesk-api-1",
    metric_store: MetricStore | None = None,
    metric_flush_seconds: float = 0,
) -> dict[str, object]:
    if concurrent not in {10, 50, 100}:
        raise ValueError("Phase 18 tests 10, 50, or 100 concurrent calls")
    started = time.monotonic()
    metric_before = metric_store.snapshot() if metric_store is not None else None
    stop = asyncio.Event()
    monitor_task = asyncio.create_task(monitor(settings, container, stop))
    try:
        prepared = await asyncio.gather(
            *(prepare(client, base_url, settings, number) for _ in range(concurrent)),
            return_exceptions=True,
        )
        ready = [item for item in prepared if isinstance(item, PreparedCall)]
        failures = [item for item in prepared if not isinstance(item, PreparedCall)]
        results = await asyncio.gather(*(drive(call, frames, hold_seconds) for call in ready))
        if metric_store is not None:
            await asyncio.sleep(metric_flush_seconds)
        ended = time.monotonic()
    finally:
        stop.set()
        resources = (await monitor_task).summary()
    first = [item.first_audio_ms for item in results if item.first_audio_ms is not None]
    after = [
        item.post_utterance_audio_ms for item in results if item.post_utterance_audio_ms is not None
    ]
    successes = sum(item.success for item in results)
    provider_metrics = (
        metric_store.summarize(metric_before, metric_store.snapshot(), started, ended)
        if metric_store is not None
        and metric_before is not None
        and metric_store.last_received_at is not None
        and metric_store.last_received_at >= started
        else None
    )
    resource_complete = all(
        resources[key] is not None
        for key in (
            "api_cpu_percent_peak",
            "api_memory_bytes_peak",
            "db_runtime_connections_peak",
            "redis_clients_peak",
            "redis_memory_bytes_peak",
        )
    )
    provider_complete = (
        provider_metrics is not None
        and provider_metrics["db_pool_checked_out_peak"] is not None
        and isinstance(provider_metrics["stt_samples"], int)
        and provider_metrics["stt_samples"] > 0
        and isinstance(provider_metrics["tts_samples"], int)
        and provider_metrics["tts_samples"] > 0
        and (
            not settings.agent_enabled
            or (
                isinstance(provider_metrics["llm_samples"], int)
                and provider_metrics["llm_samples"] > 0
            )
        )
    )
    return {
        "concurrency_requested": concurrent,
        "concurrency_connected": len(ready),
        "successes": successes,
        "errors": concurrent - successes,
        "error_rate": (concurrent - successes) / concurrent,
        "preparation_errors": len(failures),
        "duration_seconds": round(time.monotonic() - started, 3),
        "first_audio_p50_ms": statistics.median(first) if first else None,
        "first_audio_p95_ms": percentile(first, 0.95),
        "post_utterance_audio_p95_ms": percentile(after, 0.95),
        "failure_stages": sorted(
            [item.error_stage or "unknown" for item in results if not item.success]
            + [failure_stage(item) for item in failures]
        ),
        "resources": resources,
        "provider_metrics": provider_metrics,
        "measurement_complete": (
            resource_complete and provider_complete and successes == concurrent
            if settings.telephony_mode == "pipeline"
            else False
        ),
    }


async def run(args: argparse.Namespace, settings: Settings) -> dict[str, object]:
    base_url = str(args.base_url)
    base = urlsplit(base_url)
    if base.scheme not in {"http", "https"} or not base.hostname or base.path not in {"", "/"}:
        raise ValueError("Base URL must be an HTTP(S) origin")
    if base.hostname not in {"localhost", "127.0.0.1", "::1"} and not args.allow_remote:
        raise ValueError("Remote targets require --allow-remote")
    if settings.environment == Environment.PRODUCTION:
        raise ValueError("Load test refuses production environment")
    if not settings.telephony_enabled or not settings.twilio_auth_token.get_secret_value():
        raise ValueError("Load test requires enabled telephony and local Twilio credentials")
    if args.hold_seconds < 10 or args.hold_seconds > 300:
        raise ValueError("Hold duration must be between 10 and 300 seconds")
    frames = audio_frames(args.wav, echo=settings.telephony_mode == "echo")
    store = MetricStore() if args.collect_otlp else None
    server = task = None
    if store is not None:
        server, task = await start_collector(store, args.otlp_host, args.otlp_port)
    try:
        results: list[dict[str, object]] = []
        if store is not None:
            await asyncio.sleep(args.otlp_warmup_seconds)
        async with httpx.AsyncClient(base_url=base_url, timeout=20) as client:
            response = await client.get("/health/ready")
            if response.status_code != 200:
                raise RuntimeError("Target is not ready")
            for level in (10, 50, 100):
                results.append(
                    await run_tier(
                        client,
                        base_url,
                        settings,
                        args.to,
                        frames,
                        level,
                        args.hold_seconds,
                        args.container,
                        store,
                        args.otlp_flush_seconds if store is not None else 0,
                    )
                )
    finally:
        if server is not None and task is not None:
            server.should_exit = True
            await task
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "mode": settings.telephony_mode,
        "agent_enabled": settings.agent_enabled,
        "audio_source": "wav" if args.wav else "echo_silence",
        "audio_duration_seconds": round(len(frames) * 0.02, 3),
        "hold_seconds": args.hold_seconds,
        "otel_collected": store is not None,
        "providers": {
            "stt": settings.primary_stt_provider,
            "stt_fallback": settings.fallback_stt_provider,
            "llm": settings.llm_provider if settings.agent_enabled else None,
            "tts": settings.primary_tts_provider,
            "tts_fallback": settings.fallback_tts_provider,
        },
        "db_pool_size": settings.db_pool_size,
        "db_max_overflow": settings.db_max_overflow,
        "tiers": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--to", required=True, help="Assigned inbound Twilio number in E.164")
    parser.add_argument("--wav", type=Path, help="8 kHz mono PCM16 speech; required for pipeline")
    parser.add_argument("--hold-seconds", type=float, default=40)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--container", default="ai-frontdesk-api-1")
    parser.add_argument("--collect-otlp", action="store_true")
    parser.add_argument("--otlp-host", default="127.0.0.1")
    parser.add_argument("--otlp-port", type=int, default=4319)
    parser.add_argument("--otlp-flush-seconds", type=float, default=3)
    parser.add_argument("--otlp-warmup-seconds", type=float, default=3)
    parser.add_argument("--output", type=Path, default=Path(".local/phase18-load.json"))
    args = parser.parse_args()
    result = asyncio.run(run(args, Settings.from_environment()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    tiers = cast(list[dict[str, object]], result["tiers"])
    if any(
        (isinstance(tier["errors"], int) and tier["errors"] > 0)
        or (result["mode"] == "pipeline" and tier["measurement_complete"] is not True)
        for tier in tiers
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
