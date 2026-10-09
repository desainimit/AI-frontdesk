# AI Frontdesk — backend

The API, voice runtime and background worker for an AI receptionist used by clinics:
it answers calls, books and changes appointments, and hands off to staff.

The codebase is being rebuilt in six phases. This README describes what works now;
it is updated as each phase lands.

## What runs

| Process | Command | Purpose |
| --- | --- | --- |
| Setup job | `python -m app.setup` | Applies migrations, provisions the restricted database logins and creates the first super admin. Safe to run again. |
| API | `uvicorn app.main:create_app --factory` | HTTP API, Twilio webhooks and the call media stream. |
| Worker | `python -m app.worker` | Every background queue: notifications, post-call reports, email, knowledge ingestion and configuration events. |

The AI model and knowledge-search embeddings use Google Gemini. Speech uses Deepgram
and ElevenLabs (Azure is an alternative for both). Telephony is Twilio.

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/) (dependencies are pinned in `uv.lock`).
- PostgreSQL 17 with the `vector` extension.
- Redis or Valkey.
- Docker with Compose v2 for the container stack.

## Configuration

Copy `.env.example` to `.env`. Never commit `.env`.

Three database logins point at the same database:

| Variable | Login | Used by |
| --- | --- | --- |
| `MIGRATION_DATABASE_URL` | Schema owner | The setup job only. |
| `DATABASE_URL` | Restricted API login | The API. Row-level security is enforced for it. |
| `WORKER_DATABASE_URL` | Restricted worker login | The worker. It can read queues, not patient records. |

The setup job creates the two restricted logins if they do not exist. URL-encode
passwords.

For the AI, set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY`, or
`LLM_PROVIDER=openrouter` and `OPENROUTER_API_KEY`. A Gemini key must come from a
billing-enabled Google project: on the free tier Google uses prompts and replies to
improve its products, which is not acceptable for patient calls.

For development email, point `SMTP_HOST` at Mailpit (see below) and read the
messages at http://localhost:8025.

## Run with Docker Compose

```sh
docker compose config --quiet
docker compose up --build -d --wait
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
```

Compose starts the setup job, the API, the worker, Redis and Mailpit (a local
mailbox: with `SMTP_HOST=mailpit`, `SMTP_PORT=1025` and `SMTP_TLS_MODE=plain` every
email is caught at http://localhost:8025 and nothing is delivered). It uses your own
PostgreSQL server; containers reach a database on the host through
`host.docker.internal`.

## Run on the host

```sh
uv sync --locked
uv run python -m app.setup
uv run uvicorn app.main:create_app --factory --reload
uv run python -m app.worker
```

## Checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Integration tests are skipped unless `RUN_INTEGRATION=true` (PostgreSQL and Redis) or
`RUN_POSTGRES_INTEGRATION=true` (PostgreSQL only). They create tenants, so point them
at a disposable database, never a development one.

## More

- [Sign-in and accounts](docs/sign-in.md)
- [Workspaces and provider connections](docs/tenant-integrations.md)
- [Fresh database setup](docs/fresh-install.md)
