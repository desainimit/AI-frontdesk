# Phase 18: concurrent-call load testing

Phase 18 measures **10, 50, and 100 simultaneous calls**. The runner sends signed synthetic Twilio incoming callbacks, opens each call's bound Media Stream WebSocket, streams 8 kHz audio at 20 ms intervals, holds the sessions open, then sends a valid stop event. It exercises the API, PostgreSQL, Redis, STT, agent, and TTS paths without placing PSTN calls. Use a disposable staging tenant and non-sensitive speech fixture: synthetic calls create real call, audit, transcript, and follow-up data, and configured AI providers may bill for usage. The command refuses `ENVIRONMENT=production` and requires `--allow-remote` for non-loopback targets.

## Setup

1. Start the existing local PostgreSQL and Docker Desktop, then apply migrations with `docker compose run --rm migrate`. This project has no PostgreSQL image. Start the stack with `docker compose up --build` and confirm `/health/ready` returns 200.
2. Assign a test phone number to a disposable tenant using the platform API. Set `TELEPHONY_ENABLED=true` and Twilio credentials in `.env`; `TELEPHONY_PUBLIC_BASE_URL` must be the same public HTTPS origin the API uses to verify signatures. The runner reads the token locally and never writes it to its report.
3. For a pipeline test, use a short, non-sensitive spoken WAV file: mono, 8 kHz, 16-bit PCM, 20 ms to 15 seconds. Use a plain clinic question that does not book or mutate records. The runner requires a WAV for pipeline mode; a silent recording will leave STT, LLM, and TTS unmeasured. For an echo transport-only run, set `TELEPHONY_MODE=echo`; no WAV is required.
4. For provider and pool metrics, set `OTEL_ENABLED=true`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4319`, and `OTEL_METRIC_EXPORT_INTERVAL_SECONDS=2` for the API, then restart it. The temporary receiver below listens on port 4319; keep it reachable only from the test environment. If the app runs directly on the host, use `http://127.0.0.1:4319` and leave the receiver bound to loopback.

Run from the workspace with the same `.env` as the API:

```powershell
.venv\Scripts\python.exe -m app.maintenance.call_load --to +15551234567 --wav .local\speech.wav --hold-seconds 40 --collect-otlp --otlp-host 0.0.0.0
```

Use an actually assigned test number in place of the example. The runner writes `.local/phase18-load.json` and exits nonzero if any call fails or a pipeline tier lacks required measurements. For a remote staging URL, add `--base-url https://staging.example.com --allow-remote`; the public Twilio origin in `.env` must still match that deployment. `--container` selects the API container for `docker stats` (default `ai-frontdesk-api-1`). If the host cannot reach PostgreSQL or Redis directly, the corresponding fields remain `null`. The host sampler maps `host.docker.internal` and the Compose `redis` hostname to `localhost` for local runs.

## Report interpretation

Each tier reports requested and connected concurrency, success/error counts and rate, preparation error stages, elapsed time, first outbound audio p50/p95 from the first inbound frame, and p95 after the spoken fixture ends. The client counts a call as successful only if it receives outbound media and the media session closes normally. First-audio timing includes the spoken audio duration; post-utterance timing is closer to perceived response delay. Both are measured at the WebSocket client, outside the API process. `measurement_complete=true` requires all calls to succeed plus CPU, memory, database, Redis, pool, STT, TTS, and (when enabled) LLM samples. Echo runs are transport checks and always have `measurement_complete=false`.

The resource sampler records peak API container CPU percentage and memory bytes from `docker stats`, PostgreSQL connections for the API login from `pg_stat_activity`, Redis connected clients, and Redis memory. PostgreSQL connection count is a host-side proxy; the `db.pool.checked_out` OTLP gauge is the direct SQLAlchemy pool measure. Missing sources are `null`, not zero.

With `--collect-otlp`, the ephemeral receiver computes **per-tier** deltas from cumulative OpenTelemetry metrics: STT live finalization latency (last inbound frame to final transcript), LLM structured-request latency, TTS pipeline time to first PCM chunk, fallback count, and peak checked-out DB connections. Histogram p50/p95 values are **bucket upper bounds**, not exact percentiles. An absent histogram has zero samples and `null` percentiles; that means the path was not measured and must not be treated as fast. The receiver discards traces and does not store prompts, transcripts, tokens, or audio. Its startup needs an unused port 4319. Keep `--hold-seconds` long enough to cover provider calls and at least one metric export interval.

Run the tiers with the intended STT, LLM, and TTS configuration. Compare all three tiers for rising p95 latency, connection saturation, Redis pressure, errors, and provider fallbacks. Repeat once with controlled primary-provider failure to measure fallback behavior. Record host CPU cores, memory limit, API replica count, provider model/region, audio fixture duration, and pool settings alongside the JSON before making a capacity claim. Provider quotas or outbound network limits may be the bottleneck; the results apply only to the measured environment and configuration.

## Current validation status

The runner, synthetic media protocol, metric receiver, and three-level orchestration have automated tests. Live 10/50/100-call results are **pending**: Docker Desktop, the API, and Redis were not running in the implementation environment. No CPU, latency, error-rate, or fallback capacity figures are claimed yet. Run the command above on disposable infrastructure and keep its generated JSON as the Phase 18 acceptance evidence.
