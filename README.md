# AI Frontdesk

The approved production SaaS is being implemented. See the current
[implementation status](docs/saas-implementation.md) and
[fresh database initialization](docs/fresh-install.md) before deployment.
The backend includes tenant isolation, platform administration, appointment domain
services, durable event delivery, Twilio media streams, STT/TTS, conversation state,
and an optional appointment agent with explicit confirmation and patient authorization.
Multilingual calls support English, Spanish, French, German, Portuguese, and Hindi,
with tenant language settings and localized confirmations.
External calendar integrations remain later phases.

See [tenant onboarding, login and provider connections](docs/tenant-integrations.md), [Phase 18 load testing](docs/phase18.md), [Phase 17 production hardening](docs/phase17.md), [Phase 16 post-call processing](docs/phase16.md), [Phase 15 durable workflows](docs/phase15.md), [Phase 14 integration interfaces](docs/phase14.md), [Phase 13 identity verification](docs/phase13.md), [Phase 12 safety engine](docs/phase12.md), [Phase 11 knowledge base](docs/phase11.md), [Phase 10 multilingual setup](docs/phase10.md), [Phase 9 appointment setup](docs/phase9.md), [Phase 8 agent setup](docs/phase8.md),
[Phase 7 state behavior](docs/phase7.md), [Phase 6 voice setup](docs/phase6.md),
[Phase 5 telephony](docs/phase5.md), [Phase 4 delivery](docs/phase4.md),
[Phase 3 domain](docs/phase3.md), and [Phase 2 authentication](docs/phase2.md).

## Requirements

- Python 3.12 (the tested runtime; `.python-version` pins this minor version).
- Your locally installed PostgreSQL server (tested with PostgreSQL 17).
- [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.12.19.
- Docker Engine/Desktop with Docker Compose v2 for the complete local stack.
- Git if you want to install pre-commit hooks or publish the repository.

Dependencies, including development tools, are pinned in `uv.lock`. Use
`uv sync --locked` to reproduce the environment. Deliberate upgrades use
`uv lock --upgrade` followed by all checks. The Docker image installs runtime
dependencies only; no credentials or `.env` files enter the build context.

## Run with your local PostgreSQL server

1. Copy `.env.example` to `.env` (`Copy-Item .env.example .env` in PowerShell).
2. Start your installed PostgreSQL service and use a dedicated development database
   for this project. Set `MIGRATION_DATABASE_URL` to its schema owner connection
   and `DATABASE_URL` to a different, restricted login such as `frontdesk_app`.
   Choose a password for that runtime login; the migration job creates it if absent.
   Set `WORKER_DATABASE_URL` to a third login such as `frontdesk_worker`, with its
   own password, for outbox delivery. All three URLs must identify the same database.
   The migration job provisions both restricted logins. Configure the optional,
   separate `FINANCIAL_DATABASE_URL` to enable financial commands; it is also
   provisioned by the migration job. URL-encode passwords.
   The example values are placeholders, not credentials.
   Containers reach your Windows host through `host.docker.internal`; `redis`
   refers to the Redis service in Compose. PostgreSQL must accept authenticated
   connections from Docker's host-facing network. If it only listens on loopback,
   use host Python below or configure the appropriate PostgreSQL interface and
   `pg_hba.conf` rule for your Docker network.
   See [Docker's host networking guide](https://docs.docker.com/desktop/features/networking/networking-how-tos/)
   for the `host.docker.internal` connection mechanism on Docker Desktop.
3. Run:

   ```sh
   docker compose config --quiet
   docker compose up --build -d --wait
   curl --fail http://localhost:8000/health/live
   curl --fail http://localhost:8000/health/ready
   ```

Compose starts the API, an outbox worker, a migration job, and Redis. It does not start or pull a
PostgreSQL image. The installed PostgreSQL server must be reachable before you
start Compose. The migration job applies `alembic upgrade head`, provisions the
restricted runtime role, and verifies it before the API starts. If it cannot
connect or privileges are unsafe, startup fails. Migration credentials are passed
only to this job, not the API or worker containers. The worker receives its own
restricted database login, with access only to outbox delivery. It is a
separate job, not a migration in each API worker. Redis must also become healthy;
its persistence is disabled because its role is ephemeral state.

The API and migration services use `pull_policy: build`: Compose builds
`ai-frontdesk:local` from this workspace instead of looking for it on Docker Hub.
The initial build can still download the Python and uv base images, plus the
Redis image. These are separate from PostgreSQL, which runs on your host.

`docker compose logs -f api worker` shows JSON operational events. The default
worker uses the console notification adapter: it logs simulated deliveries and
does not send SMS or email. Real provider adapters remain later integration work.
`docker compose down` stops the containers; PostgreSQL remains managed by your
local installation. If port 6379 is occupied, change the Redis host-side published
port; its container URL keeps port 6379. Set the PostgreSQL port in `DATABASE_URL`
to match your installed server.

Compose is for local development: published ports bind to loopback, Redis has no
password, and your migration role needs schema and role-creation permissions. Production requires TLS,
managed secrets, private networks, backups, a restricted application database
role, and a separate migration role. Those deployment controls are not established
by this local stack.

## Run Python on the host

```sh
uv sync --locked
docker compose up -d redis
```

For host execution set all three PostgreSQL URLs to `postgresql+asyncpg://...@localhost:5432/...`
and `REDIS_URL` to `redis://localhost:6379/0` in the shell, or in `.env`. If you edit
`.env`, restore `host.docker.internal` for PostgreSQL and `redis` for Redis before
starting the Compose API again. Host Python connects directly to your installed
PostgreSQL service; Docker is only needed here if you use it to run Redis.

```sh
uv run --locked python -m app.database.initialize
uv run --locked uvicorn app.main:create_app --factory --reload --no-access-log
```

Local/development API documentation: <http://localhost:8000/docs>.
Staging and production disable the docs and OpenAPI endpoints. Both health
endpoints remain unauthenticated for infrastructure probes and must be routed
appropriately by your deployment.

## Checks

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pyright
uv run --locked pytest
uv build
```

Unit/API tests run without network services. Real dependency tests are explicitly
skipped unless `RUN_INTEGRATION=true`; a skipped integration test is not evidence
that PostgreSQL or Redis works. Use a disposable database and Redis instance,
set the host connection URLs, then run in PowerShell:

```powershell
$env:RUN_INTEGRATION = 'true'
uv run --locked alembic upgrade head
uv run --locked python -m app.database.provision
uv run --locked alembic check
uv run --locked pytest
```

On POSIX shells use `export RUN_INTEGRATION=true`. Integration tests verify real
connections, migration state, independent sessions, transaction rollback, and
readiness. They create a transaction-scoped probe table and roll it back. Never
point tests at a production database. CI always enables integration tests with
disposable PostgreSQL/Redis service containers, and separately builds and starts
the Compose stack and probes both endpoints. Hosted CI uses the explicit
`docker-compose.ci.yml` overlay to supply its own disposable PostgreSQL; ordinary
local `docker compose` commands do not load that overlay.

For PostgreSQL-only verification, set `RUN_POSTGRES_INTEGRATION=true` instead;
this explicitly leaves the real Redis/full-readiness test skipped.

After initializing Git, run `uv run --locked pre-commit install`. Hooks run Ruff
and strict Pyright through the locked environment. Equivalent Makefile targets
are provided where `make` is available. Configure branch protection in your
GitHub repository to require both CI jobs; a workflow file alone cannot enforce
merge protection.

## Runtime behavior and architecture

- `app/main.py` creates an application; importing it does not open connections.
  Lifespan owns the database pool, Redis pool, and telemetry. An async exit stack
  cleans up resources in reverse order, including partial startup failure.
- `app/config/settings.py` validates environment configuration through Pydantic
  Settings. Environment variables override `.env`. Required connection strings
  use `SecretStr`; invalid configuration stops startup. Production secrets should
  be injected by the deployment secret manager, never committed or baked in.
- `app/database/session.py` creates a new async SQLAlchemy session per caller.
  Session exit closes/rolls back uncommitted work. There is no global session and
  no implicit commit on a bare session. `UnitOfWork.transaction(context)` sets
  transaction-local tenant scope, verifies role/membership, and commits or rolls
  back atomically. RLS is enabled and forced on all eleven tenant-owned tables.
  A role-specific policy grants only the worker access to queued events across tenants. Pool pre-ping
  handles stale connections. Tenant services receive context explicitly.
- `/health/live` returns `200 {"status":"alive"}` without touching dependencies.
  `/health/ready` concurrently executes PostgreSQL `SELECT 1` and Redis `PING`,
  and verifies the database role/schema can enforce tenant isolation, returning
  200 when both succeed or 503 with named `unavailable` checks. Probes
  have bounded timeouts, are not cached, and recover on the next successful probe.
  Dependency outages do not prevent the process from serving liveness.
- Application, validation, HTTP, and unexpected errors use a consistent JSON
  envelope with a request ID. Responses exclude exception details and invalid
  input values. Valid UUID `X-Request-ID` headers are preserved; other values are
  replaced. Request IDs identify requests, not authenticated callers or tenants.
- JSON logs allow-list operational fields. Application log messages must be
  static event names. Do not interpolate user content. Arbitrary extra fields,
  third-party message text, SQL parameters, exception text, bodies, headers,
  query strings, and raw paths are not emitted. Error types and correlation IDs
  remain available for diagnosis. Verified tenant/call IDs are included in request
  logs. Durable audit events are stored separately in PostgreSQL.
- `app/observability/tracing.py` owns OpenTelemetry trace and metric providers.
  `OTEL_ENABLED=true` enables batched OTLP/HTTP export to the configured base URL
  (`/v1/traces`, `/v1/metrics`). Optional headers use comma-separated `key=value`
  syntax with percent-encoded values. `OTEL_TRACE_SAMPLE_RATIO` controls root span
  sampling; valid W3C parent trace context is propagated. Request duration metrics
  and spans contain method, resolved route template, and status only. Exporters
  shut down off the event loop. Collector deployment/dashboards/alerts are deferred.
  Telemetry is implemented with explicit ASGI middleware rather than automatic
  request instrumentation to keep exported attributes within this allow-list.

## Migrations

`0001_foundation` establishes Alembic's version table. `0002_multitenancy` adds
tenants, users, API credentials, audit events, RLS policies, and audit immutability
triggers. All future schema changes must use Alembic. Register future models in
`app/database/models.py` when their phase is implemented. Alembic uses
`MIGRATION_DATABASE_URL`; runtime credentials must not own the tables.

```sh
uv run --locked alembic revision --autogenerate -m "describe change"
uv run --locked alembic upgrade head
uv run --locked alembic check
uv run --locked alembic current
```

Review generated revisions before applying them. Use expand/contract migrations
when old and new application versions overlap. Rehearse restore and rollback on
disposable data before deployment. Prefer a forward correction for destructive
schema changes; downgrade is not a backup strategy. The baseline can be reverted
with `alembic downgrade base` on a disposable database. This command clears its
version marker; it does not represent a future production rollback procedure.
Once Phase 2 is applied, downgrading removes tenant/user/credential/audit tables
and their data. Do not downgrade a populated database without an approved restore plan.

## Files introduced in Phase 1

```text
app/
  main.py, resources.py
  api/{dependencies,errors,health,middleware}.py
  cache/redis.py
  config/{settings,logging}.py
  database/{base,session}.py
  observability/tracing.py
migrations/{env.py,script.py.mako,versions/0001_foundation.py}
tests/                         # API, config, logging, lifecycle, telemetry, integration
.github/workflows/ci.yml
.env.example, .gitignore, .dockerignore, .python-version
.pre-commit-config.yaml
Dockerfile, docker-compose.yml, docker-compose.ci.yml, Makefile
pyproject.toml, uv.lock, alembic.ini
README.md, docs/validation.md
```

Phase 2 adds `app/auth/`, `app/tenants/`, `app/users/`, `app/audit/`,
`app/database/{types,models,rls,unit_of_work,provision,initialize}.py`,
`app/api/tenancy.py`, `app/errors.py`, the `0002_multitenancy` migration,
`tests/test_authorization.py`, and `tests/integration/test_tenancy.py`.
Existing application wiring, settings, logging, health validation, Compose, CI,
and database tests have been updated to use separate database roles.

Existing `Plan.md` is unchanged. No provider SDK or later-phase placeholder module
has been added. See [validation evidence](docs/validation.md) for the checks actually
run in the implementation environment and outstanding acceptance checks.

Implementation references: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/),
[SQLAlchemy async sessions](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html),
[Redis asyncio](https://redis.readthedocs.io/en/stable/examples/asyncio_examples.html), and
[OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/).
