# Phase 17: production hardening

Apply `0014_hardening` with the existing migration job (`docker compose run --rm migrate`). It adds the tenant/creation-time index used by transcript retention. Use the existing PostgreSQL installation; Compose does not start or pull a PostgreSQL image.

## Request protection

Set `RATE_LIMITS_ENABLED=true` for staging and production. Production startup rejects a disabled rate limiter. The Redis-backed counter is atomic across API replicas and uses hashed, authenticated identities: 240 tenant API requests per actor per minute, 120 platform requests per super-admin per minute, and 60 signed Twilio callbacks per call per minute by default. Tune these values from observed traffic. Redis errors cause protected requests to return 503; an exhausted limit returns 429. `/health/ready` reports Redis or PostgreSQL failure as 503. Put a separate IP-based limit at the public ingress for invalid credentials and unsigned requests, which cannot be safely attributed to an authenticated actor.

Twilio HTTP callbacks are accepted only after signature, account, form, and call-ID checks. Twilio signatures authenticate a request but do not establish a timestamp. Duplicate incoming callbacks reuse the same call record only during the five-minute pre-stream window; an active or expired call cannot create another stream. Stream startup is single-use and terminal callbacks preserve the first terminal result. Avoid rejecting all duplicate callbacks at ingress, since legitimate provider retries must remain idempotent. Keep `TELEPHONY_PUBLIC_BASE_URL` equal to the exact public HTTPS origin used by Twilio, including through the reverse proxy.

## Capacity and failures

The API uses pre-ping, a bounded pool, LIFO reuse, 30-minute connection recycle, five-second pool wait, and ten-second SQL command timeout by default. Budget PostgreSQL connections across **every** API and worker replica: `replicas × (DB_POOL_SIZE + DB_MAX_OVERFLOW)`, plus maintenance and admin connections. Set `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_SECONDS`, `DB_POOL_RECYCLE_SECONDS`, and `DB_COMMAND_TIMEOUT_SECONDS` from measured concurrency and the server's connection limit. Redis uses bounded connection and socket timeouts. Existing voice STT/TTS circuit breakers and provider timeouts remain enabled. API lifespan closes providers, Redis, telemetry, and the DB pool; Compose gives the API 30 seconds and workers 75 seconds to stop.

For a bounded HTTP smoke probe, run:

```powershell
.venv\Scripts\python.exe -m app.maintenance.load_probe --url http://127.0.0.1:8000/health/live --requests 100 --concurrency 10
```

It prints successes, failures, elapsed time, and p50/p95 latency; any non-200 or request error makes the command fail. This checks HTTP handling only. Phase 18 separately measures 10, 50, and 100 **real concurrent calls** with provider latency, CPU, memory, DB pool, and Redis metrics. Inject a Redis outage in staging and confirm 503 on protected routes and readiness, restore Redis, then inject STT/TTS failures and confirm circuit fallback and no stalled calls. Check `docker compose logs api worker` for secret-free errors. Run `pytest tests/test_hardening_phase17.py tests/test_telephony.py tests/test_health.py tests/test_lifespan.py` and, with disposable PostgreSQL/Redis, set `$env:RUN_INTEGRATION='true'` before `pytest tests/integration/test_telephony.py`.

## Backup and restore

Run backup commands **on an operator host** with matching PostgreSQL client tools (`pg_dump`, `pg_restore`, `createdb`, `psql`) installed. The app image intentionally has no database client binaries. Supply a schema-owner or dedicated backup account through `MIGRATION_DATABASE_URL` in a protected local `.env` or injected environment; for host commands use the host's PostgreSQL address rather than `host.docker.internal`. Do not place passwords in CLI arguments or logs.

```powershell
.venv\Scripts\python.exe -m app.maintenance.backup backup --directory D:\secure-frontdesk-backups
.venv\Scripts\python.exe -m app.maintenance.backup verify-restore --backup D:\secure-frontdesk-backups\frontdesk-YYYYMMDDTHHMMSSZ-XXXXXXXX.dump --target-database frontdesk_restore_test_20261002
```

Backup writes a custom-format archive and SHA-256 manifest. Restore verification checks the manifest, creates a **new** `frontdesk_restore_test_*` database, restores transactionally, then checks the Alembic revision, pgvector extension, and forced RLS on core tenant tables. The restore server must have pgvector installed. A pre-existing restore target fails at `createdb`; the command never drops a database. After review, remove the test database manually. Schedule encrypted, access-controlled offsite backups and monthly restore drills, with a documented recovery point/time target. Retain database backups at least 30 days (or longer where law or contract requires); protect the separate S3 knowledge objects and any other external stores with their own versioned backups. A database dump alone cannot recover those objects. The command does not schedule, encrypt, upload, or expire backup files.

## Data and audit retention

The per-tenant retention job defaults to a dry run. It removes transcript snapshots after 30 days and post-call reports after 365 days, in batches of at most 1,000 per table. Reports with pending follow-up events remain. Run it with the restricted `WORKER_DATABASE_URL` and an explicit tenant ID; repeat until zero eligible rows remain:

```powershell
.venv\Scripts\python.exe -m app.maintenance.retention --tenant-id <TENANT_UUID>
.venv\Scripts\python.exe -m app.maintenance.retention --tenant-id <TENANT_UUID> --apply
```

Call metadata, appointment records, and immutable `audit_events` are outside this purge. Keep audit events online for at least seven years, or a longer jurisdiction-specific period; include them in protected backups and monitor table growth. The database trigger rejects update/delete/truncate of audit rows, so this release does **not** silently erase them. Any future audit archival/deletion workflow needs a separate approved design that preserves integrity and legal holds. The chosen 30/365-day defaults and seven-year audit minimum are operational policy examples; review local retention law and tenant contracts before scheduling the job.

## Secrets and security review

Inject production credentials from a secret manager into process environment or mounted secret files; restrict the migration credential to the migration/backup job. Keep `.env` for local development only and out of Git, images, logs, and artifacts. Rotate the Twilio token, database roles, Redis password, and provider API keys through the owning service, redeploy, then verify webhook signatures and readiness. Restrict backups and their manifests as sensitive data. Before release, review ingress limits and TLS, run dependency/image scans, test malformed and replayed Twilio callbacks, verify RLS with disposable integration infrastructure, and perform a restore drill. The current local checks alone do not certify the deployment or load capacity.
