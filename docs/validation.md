# Validation record

## Phase 9 acceptance audit - 2026-09-30

- Added a booking scenario through the Twilio media WebSocket with scripted STT,
  TTS and agent responses plus real PostgreSQL. It exercises access-code redemption,
  proposal, explicit voice confirmation, one committed appointment and terminal
  conversation state. No external provider call was made.
- Complete regression suite: **224 passed, zero skipped** after local Redis was
  started. Ruff lint, format checks and strict Pyright passed. The migration upgrade,
  schema comparison, downgrade and re-upgrade passed on disposable PostgreSQL.
- The only warning is Python 3.12's existing `audioop` deprecation.

The earlier Phase 9 entry below records the prior 223-test run. Live Twilio and
provider acceptance still needs configured credentials and a real call.


## Phase 9 appointment agent - 2026-09-29

- Full suite: **223 passed, zero skipped**, against disposable native PostgreSQL 17
  and local Redis. The test cluster was stopped afterward; the application database
  was not modified and no PostgreSQL image was pulled.
- Ruff lint/format checks passed; strict Pyright reported zero errors and warnings.
- Migration `0007_appointment_agent`: upgrade, metadata comparison, full downgrade
  to base, and re-upgrade passed. Runtime role provisioning passed.
- Docker API image build and Compose configuration validation passed.
- With networking disabled, the built image passed application imports, appointment
  tool registration/schema validation, and deterministic confirmation smoke checks.
- Appointment tests cover changed-time proposals, explicit confirmation, cancellation,
  rescheduling, expired confirmation, occupied slots, committed-response replay,
  concurrent duplicate requests on separate connections, patient ownership, forged
  identity, cross-tenant access, code expiry/lockout, and reception endpoint authentication.
- Standalone access codes bypass the LLM and are redacted before conversation persistence.

The sole warning is the existing Python 3.12 `audioop` deprecation. No real Twilio,
STT, TTS, or LLM calls were made. Reception-attested access is the Phase 9 bridge;
automated identity verification remains Phase 13. See [Phase 9 setup](phase9.md).


## Phase 5/6 corrections and Phase 7/8 verification - 2026-09-29

- Full suite: **195 passed, zero skipped**, using disposable native PostgreSQL 17
  and local Redis. No PostgreSQL image was pulled or existing application database modified.
- Ruff lint and formatting passed; strict Pyright reported zero errors and warnings.
- Alembic upgrade, metadata check, downgrade to base, and re-upgrade passed.
  Migration head remains `0006_telephony`; these corrections add no migration.
- Docker API image built successfully with production HTTP/WebSocket dependencies.
  With networking disabled, application imports, the mock agent, and real Pipecat
  echo processing passed inside the image.
- New voice regressions cover live recognition via a local WebSocket server,
  Azure 16 kHz WAV utterances, background error propagation, concurrent fallback
  and single replay, PCM chunk boundaries, SSML escaping, and playback marks/barge-in.
- Phase 7 conversation and Phase 8 agent regressions remain included in the full suite.

The sole test warning is Python 3.12's `audioop` deprecation. External provider
responses were simulated; live Twilio, STT, TTS, and LLM calls were not performed.
See [Phase 5](phase5.md), [Phase 6 corrections](phase6.md), and [Phase 8](phase8.md).
Earlier entries below are historical and are superseded by this verification.

## Phase 7 review and Phase 8 - 2026-09-28

- Full suite against a disposable native PostgreSQL 17 database and local Redis:
  **182 passed, zero skipped**.
- Ruff lint and formatting: passed. Strict Pyright: zero errors.
- Alembic upgrade, metadata check, downgrade to base, and re-upgrade: passed.
  Phase 8 adds no migration; head remains `0006_telephony`.
- Docker Compose configuration and API image build: passed.
- Media regression checks cover both agent-disabled and agent-enabled behavior,
  persisted terminal conversation state, and generated responses rather than STT echo.
- Provider contract tests use a mocked HTTP transport with the actual OpenAI SDK,
  including structured request format, refusal, incomplete output, and rate limits.
- Tool tests cover successful reads, unknown tools, malformed/extra/coerced arguments,
  duplicate JSON keys, tenant/call mismatch, safety blocks, cancellation, timeout,
  step/tool budgets, audit records, and authorization after call completion.
- Conversation regression tests cover ambiguous/negative confirmation, stale
  parameters, identity lockout, language stability, Redis failures and key binding.

The only full-suite warning is Python 3.12's existing `audioop` deprecation.
No real Twilio, STT, TTS, or LLM call was made. Live-provider acceptance still
requires configured credentials and an actual phone call.
See [Phase 7 review](phase7.md) and [Phase 8 setup](phase8.md).


## Phase 6 — current, 2026-09-27

Phase 6 implements production-ready Speech-to-Text (STT) and Text-to-Speech (TTS) with Deepgram and ElevenLabs as primary providers, Azure Cognitive Services Speech as automatic fallback, circuit breaker isolation, caller barge-in interruption detection via audio energy RMS analysis, Twilio `clear` buffer flushing, and OpenTelemetry observability.

| Check | Observed result |
| --- | --- |
| Ruff lint and formatting | Passed: `ruff check .` (0 errors) |
| Strict Pyright | Passed: 0 errors, 0 warnings across whole repository |
| Phase 6 unit & pipeline suite (`tests/test_voice_phase6.py`) | **13 passed, zero failures** |
| Full unit test suite | **78 passed, 52 intentionally skipped** |
| Circuit breaker lifecycle & state transitions | Passed (`CLOSED` -> `OPEN` on 3 consecutive failures; `HALF_OPEN` trial; recovery) |
| Deepgram STT provider & stream | Passed (HTTP streaming, transcription parsing, typed error mapping: 401, 429, 503, timeout) |
| Azure STT provider & stream | Passed (Simple & detailed NBest parsing, typed error mapping, fallback compatibility) |
| STT fallback failover | Passed (Primary transient failure immediately switches to secondary; metrics & circuit updated) |
| ElevenLabs TTS provider & stream | Passed (Chunk streaming, typed error mapping: 401, 429, 503, timeout) |
| Azure TTS provider & stream | Passed (SSML generation, chunk streaming, typed error mapping) |
| TTS fallback failover | Passed (Failover before first chunk routes smoothly to secondary engine) |
| Barge-in interruption handler | Passed (RMS audio energy voice activity detection, consecutive frame threshold, cancel trigger) |
| Twilio buffer clearance | Passed (Barge-in dispatches `{"event": "clear", "streamSid": "..."}` to flush Twilio queue) |
| VoicePipeline full loopback | Passed (Inbound µ-law -> PCM16 -> STT -> Agent -> TTS -> PCM16 -> µ-law outbound) |
| Voice factory dynamic assembly | Passed (Provider wiring with fallback rules and `none` override) |
| Telephony WebSocket pipeline mode | Passed (`run_media` executes `pipeline` mode with full stream lifecycle and cleanup) |
| Zero-PII observability | Verified: Audio payloads and transcripts excluded from structured logs and metrics |

See [Phase 6 STT and TTS documentation](phase6.md).

## Phase 5 — historical, 2026-09-27

Phase 5 implements the telephony foundation: inbound Twilio webhooks, HMAC signature verification, bidirectional Twilio Media Streams, Pipecat audio echo pipeline, call lifecycle/state management, and `CallContext`. Migration `0006_telephony` adds `phone_numbers` and `calls` tables with tenant isolation, lookup, and platform administration RLS policies.

| Check | Observed result |
| --- | --- |
| Ruff lint and formatting | Passed |
| Strict Pyright | Passed: 0 errors, 0 warnings |
| In-memory unit suite (`pytest tests/test_telephony.py`) | **11 passed, zero skipped** |
| Default unit suite | 65 passed, 52 intentionally skipped |
| Twilio webhook signature verification (HTTP & WebSocket) | Passed (valid signed passes, tampering/mismatch rejected with 403) |
| Webhook payload validation & origin check | Passed (destination E.164, AccountSid match, HTTPS origin required) |
| Media stream security binding (HMAC-SHA256 token) | Passed (constant-time verification of tenant, call, and CallSid binding) |
| Inbound phone number tenant resolution & RLS isolation | Passed (inbound lookup by dialed number; calls isolated by tenant) |
| Pipecat audio echo pipeline (`EchoSession`) | Passed (8000Hz 8-bit µ-law <-> PCM16 conversion, bidirectional frame echo) |
| Monotonic stream sequence and disconnect cleanup | Passed (`stop`, `disconnect`, `invalid_stream`, `stream_timeout` transitions) |
| Frame counting and terminal state persistence | Passed (`received_frames`, `sent_frames`, `ended_at`, `end_reason` recorded) |
| Audit trail on telephony actions | Passed (`call_received`, `call_started`, `call_ended`, `calls_viewed`, `phone_number_assigned`) |
| Platform phone provisioning & tenant list endpoints | Passed (super-admin access enforced, tenant read-only call access) |
| Alembic schema comparison (`alembic check`) | Passed: No new upgrade operations detected |
| Docker build, compose services, and health probes | API, Redis, and Worker healthy; Ready probe 200 OK |
| OpenAPI schema inspection | Telephony endpoints exposed |

Tests verified audio frame encoding/decoding, HMAC token validation, call lifecycle state machine, advisory locking on call receipt, tenant isolation, and clean disconnects. See [Phase 5 operation and testing](phase5.md).

## Phase 4 — historical, 2026-09-27

Phase 4 implements the reliability acceptance criteria on top of the existing
domain and platform-admin code. Local PostgreSQL remains the database; the new
worker uses a separate restricted login configured in `.env`.

| Check | Observed result |
| --- | --- |
| Ruff lint and formatting | Passed |
| Strict Pyright | Passed: 0 errors, 0 warnings |
| Full suite with real PostgreSQL and Redis | **99 passed, zero skipped** |
| Default suite without integration opt-in | 54 passed, 45 intentionally skipped |
| Concurrent identical booking requests | One appointment, one audit mutation, one outbox event; matching responses |
| Changed payload with a reused key | Conflict; no duplicate mutation |
| Cancellation/reschedule/original booking replay | Original response snapshots preserved |
| Failed outbox insertion | Appointment, audit mutation, and key rolled back |
| Provider failure after commit | Appointment remains booked; event retries |
| Retry success and exhaustion | Delivered on recovery; dead event retained on exhaustion |
| Provider timeout/permanent/unexpected failure | Typed, bounded, redacted outcomes |
| Multiple workers and aggregate ordering | One claim per event; creation before cancellation |
| Delivery before crash / lost acknowledgment | Stable downstream key; expired lease recovered; stale acknowledgment rejected |
| RLS and database-role separation | Tenant isolation preserved; worker cannot access clinical tables or rewrite payloads |
| Alembic schema comparison | No pending operations |
| Downgrade/base/re-upgrade | Passed on disposable PostgreSQL only |
| Docker build and local migration/provisioning | Passed |
| Local API, Redis, and worker | Healthy |
| Live HTTP probes | Live and ready returned 200 |
| Live OpenAPI | Appointment mutations expose `Idempotency-Key` |
| Existing platform super admin | Bootstrap reported existing account unchanged |
| Hosted CI | Updated for worker credentials; not executed remotely |

Tests used disposable clusters built from installed PostgreSQL 17 binaries and
real Redis. No test tenants/patients or downgrade operations were applied to the
application database. The local migration applied only forward schema changes and
role grants. A generated worker password was stored locally without displaying it;
existing API, migration, and super-admin credentials were preserved. No local
PostgreSQL image was pulled.

External delivery is simulated by the console adapter. Failure, redelivery, and
downstream idempotency behavior were tested with controlled providers, not real
SMS/email services. Delivery is at least once; real adapters must honor event IDs
at their side-effect boundary. See [Phase 4 operation and testing](phase4.md).

## Phase 3 — historical, 2026-09-27

Phase 3 implements and verifies the domain-layer acceptance criteria. The local
Compose API runs the Phase 3 image against the installed PostgreSQL database.

| Check | Observed result |
| --- | --- |
| Ruff lint and formatting | Passed |
| Strict Pyright | Passed: 0 errors, 0 warnings |
| Full suite with real PostgreSQL and Redis | **67 passed, zero skipped** |
| Default suite without integration opt-in | 43 passed, 24 intentionally skipped |
| HTTP create location/provider/patient and define availability | Passed |
| HTTP slots, booking, duplicate rejection, reschedule, cancellation | Passed |
| Concurrent bookings using a pool of four connections | One success, one conflict, one creation audit |
| Failed reschedule and audit rollback | Original appointment preserved / failed creation rolled back |
| Optimistic versions, cancelled/past states, time validation | Passed |
| Domain RLS acceptance | Unfiltered queries isolate both tenants across all five new tables |
| Composite foreign keys and direct SQL overlap exclusions | Passed |
| Member permissions and cross-tenant API references | Passed |
| Alembic upgrade and metadata comparison | Passed; no pending operations |
| Downgrade to base and re-upgrade | Passed in disposable PostgreSQL only |
| Docker image build | Passed with locked runtime dependencies, including tzdata |
| Local forward migration and runtime provisioning | Passed |
| Local Compose API and Redis | Healthy |
| Live HTTP health probes | Live and ready returned 200; PostgreSQL and Redis both `ok` |
| Live OpenAPI and unauthenticated appointment endpoint | New routes present; appointment request returned 401 |
| Hosted GitHub Actions | Not executed remotely |

Integration tests used isolated PostgreSQL clusters created with installed
PostgreSQL 17 binaries and real Redis. Those clusters were stopped afterward.
The application database received the forward migration and runtime grants;
test patients, tenants, and downgrade operations were confined to disposable
databases. No local PostgreSQL image was pulled. `.env` credentials were preserved.

The REST lifecycle ran through FastAPI's HTTP test client with real PostgreSQL;
live container checks separately verified health, route registration, and the
authentication boundary. See [Phase 3 setup and manual walkthrough](phase3.md).
General idempotency and outbox work remain Phase 4.

## Phase 2 — historical, 2026-09-27

Phase 2 is implemented and its tenant-isolation acceptance gate passes. The
running local Compose stack uses the installed PostgreSQL `frontdesk` database
with separate schema-owner and restricted runtime logins. No tenant or admin
credential was seeded automatically. Phase 3 had not started at this checkpoint.

| Check | Observed result |
| --- | --- |
| Ruff lint and formatting | Passed |
| Strict Pyright | Passed: 0 errors, 0 warnings |
| Full test suite, real PostgreSQL and Redis | **41 passed, zero skipped** |
| RLS acceptance gate | Passed: A/B queries without tenant WHERE clauses on all four tables |
| Pool reuse, missing scope, rollback | Passed; no context leakage |
| Cross-tenant inserts and foreign keys | Passed |
| Credential forgery, expiry, revocation, disabled identities | Passed |
| HTTP permissions and tenant-header mismatch | Passed |
| Audit commit/rollback and UPDATE/DELETE/TRUNCATE rejection | Passed |
| Alembic metadata check | Passed: no pending operations |
| Downgrade/base/upgrade | Passed on disposable PostgreSQL only |
| Docker image build and local migration/provision job | Passed |
| Compose startup | Passed: API and Redis healthy |
| Live HTTP probes | `/health/live` and `/health/ready` returned 200 |
| Live protected endpoints without credentials | User and tenant endpoints returned 401 |
| Hosted GitHub Actions | Updated, not executed remotely |
| Optional API container environment inspection | Not executed: automatic approval review hit a usage limit |

Tests used a disposable PostgreSQL cluster created with locally installed binaries
and real Redis. The cluster was stopped afterward. The application database received
only the forward Phase 2 migration and runtime-role provisioning, not test tenants
or downgrade operations. No local PostgreSQL image was pulled.

The local `.env` was updated to separate credentials; prior contents were backed
up to ignored `.local/phase2.env.backup`. Neither file is in the Docker build context.
Compose passes migration credentials only to the migration job. The optional final
inspection of the actual API container environment was blocked by approval-review
availability, so that separate inspection is not claimed as passed.

See [Phase 2 architecture and operator instructions](phase2.md). No new third-party
dependency was required. At this checkpoint the application was a Phase 2 foundation, not the
fully production-ready frontdesk described by the complete build plan.

## Phase 1 — historical validation

Local setup updated on 2026-09-27 to use the installed PostgreSQL service.
The default Compose file no longer contains a PostgreSQL container or volume;
the example container connection uses `host.docker.internal`. Hosted CI supplies
its disposable PostgreSQL through an explicit `docker-compose.ci.yml` overlay.
This configuration has not yet been connected to the user's existing database;
the PostgreSQL results below were obtained with an isolated local test cluster.

Validated on Windows with Python 3.12.10 on 2026-09-26. This initial run left the
Redis/Docker gate open. Both were verified on the live stack before Phase 2 work;
the historical limitations below are superseded by the current results above.

| Check | Observed result |
| --- | --- |
| Ruff lint | Passed: `ruff check .` |
| Ruff formatting | Passed: `ruff format --check .` |
| Strict Pyright | Passed: 0 errors, 0 warnings |
| Unit/API tests without services | 18 passed, 3 integration tests skipped |
| Tests with isolated PostgreSQL 17.5 | 20 passed, 1 Redis/full-readiness integration test skipped |
| Alembic online upgrade | Passed against disposable PostgreSQL |
| Alembic metadata check | Passed: no new upgrade operations detected |
| Alembic downgrade to base, then upgrade | Passed against disposable PostgreSQL |
| Alembic offline upgrade/downgrade SQL | Passed in automated tests |
| Python source distribution and wheel | Built successfully with `uv build` |
| Distribution contents | Checked: no `.env`, `.local`, or `.venv` content |
| Dependency lock consistency | Passed: `uv lock --check --offline` |
| pre-commit configuration | Passed: `pre-commit validate-config` |
| Compose and workflow YAML | Parsed successfully; this is not a Docker validation |
| Docker image / Compose startup | Not run: Docker is not installed |
| Real Redis connection | Not run: Redis and WSL are not installed |
| Hosted GitHub Actions | Configured, not executed; workspace has no Git remote |

The PostgreSQL tests used a newly initialized cluster on loopback port 55432 with
a generated password. They did not connect to or modify the installed PostgreSQL
service's database. The test cluster was stopped afterward. One non-failing
pytest cache warning occurred because the sandbox and elevated test process used
different Windows identities; it did not affect the 20 passing tests.

The automated regressions cover:

- Liveness without dependency access.
- PostgreSQL/Redis readiness failures, timeout cancellation, and recovery.
- Request ID validation, propagation, and safe error envelopes.
- Lifespan cleanup, partial startup failure, and one resource failing to close.
- Secret settings and allow-listed JSON logs.
- OpenTelemetry SDK initialization and cleanup, W3C trace parent propagation,
  error status, and exclusion of raw paths, query strings, and exception content.
- Real SQLAlchemy/asyncpg connections, fresh sessions, UTC connection timezone,
  migration state, and rollback of uncommitted PostgreSQL DDL.

To finish the acceptance gate, run the Docker commands and integration suite in
[README.md](../README.md) on a Docker-capable machine, or run the configured CI
jobs after pushing to GitHub. The CI jobs include real PostgreSQL and Redis,
migrations, strict quality checks, package build, Docker image build, Compose
startup, and live/ready HTTP probes. Require both jobs in branch protection.

Implementation choices within this phase: uv supplies a reproducible dependency
lock; the migration baseline intentionally contains no domain tables; custom
ASGI telemetry records only allow-listed attributes instead of enabling automatic
request instrumentation. These choices preserve the plan's module boundaries
and defer tenant models/RLS to Phase 2.
