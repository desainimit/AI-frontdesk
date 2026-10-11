# AI Frontdesk — backend

The API, voice runtime and background worker for an AI receptionist used by clinics:
it answers calls, books and changes appointments, and hands off to staff.

The codebase is being rebuilt in six phases. This README describes what works now;
it is updated as each phase lands.

## What runs

| Process | Command | Purpose |
| --- | --- | --- |
| Setup job | `python -m app.setup` | Applies migrations, provisions the restricted database logins and creates the first super admin. Safe to run again. |
| API | `uvicorn app.main:create_app --factory` | HTTP API, Twilio webhooks, the call media stream, and the receptionist's replies to calls and text messages. |
| Worker | `python -m app.worker` | Every background queue: messages to patients, calendar sync, account email, post-call reports, knowledge ingestion and transcript retention. |

The AI model and knowledge search use Gemini models (directly from Google, or through
OpenRouter). Speech uses Deepgram and ElevenLabs (Azure is an alternative for both).
Telephony is Twilio.

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

### Provider accounts are set in the product, not here

The phone service, speech, the AI model and the email server have no environment
variables. A platform administrator signs in and saves them under **Platform,
Settings**; each account is checked with its provider as it is saved, stored encrypted
in the database, and used by the API and the worker within a few seconds. The
Workspaces page lists what is still missing. A clinic may connect its own account;
without one it is served by the platform account.

A Gemini key must come from a billing-enabled Google project: on the free tier Google
uses prompts and replies to improve its products, which is not acceptable for patient
calls.

For development email, save the email server as host `mailpit` (or `127.0.0.1` when
running on the host), port `1025`, "No encryption", and read the messages at
http://localhost:8025.

See [clinic settings and default data](docs/settings.md) for what a new clinic starts
with and how settings are saved, and [creating clinics, patient messages and calendar
sync](docs/clinics-notifications-calendars.md) for the rest of what happens around a
booking.

## Run with Docker Compose

```sh
docker compose config --quiet
docker compose up --build -d --wait
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
```

Compose starts the setup job, the API, the worker, Redis and Mailpit (a local
mailbox: every email sent to it is caught at http://localhost:8025 and nothing is
delivered). It uses your own
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

- [Runbook: deploying, upgrading, backups, what to watch](docs/runbook.md)
- [The receptionist: calls, text messages, tickets](docs/assistant.md)
- [Sign-in and accounts](docs/sign-in.md)
- [Workspaces and provider connections](docs/tenant-integrations.md)
- [Fresh database setup](docs/fresh-install.md)
