# Runbook

How to put AI Frontdesk on a server, keep it running, and put it right when something
is wrong. Written for someone who has not seen the code.

## What runs

| Part | What it does |
| --- | --- |
| Website | What people sign in to. The only thing browsers talk to. |
| API | Answers the website, and the phone service: calls, the live audio of a call, texts. Writes the receptionist's replies. |
| Worker | Everything that is not immediate: messages and reminders, calendar sync, reading uploaded documents, call reports, deleting old transcripts. |
| Setup job | Runs once at every start: database changes, the two restricted database logins, the first administrator. |
| Redis | Live calls and rate limits. Nothing in it has to survive a restart. |
| Proxy | HTTPS for both addresses. Gets and renews the certificates. |
| PostgreSQL | Everything that must be kept. Not part of the stack: use a managed PostgreSQL 17 with the `vector` extension. |

## Before the first start

1. **Two addresses**, both pointing at the server: one for the website
   (`app.example.com`) and one for the phone service (`api.example.com`). The second
   only answers the phone service's webhooks and the health checks; everything else on
   it answers 404.
2. **A database**: PostgreSQL 17, an empty database, the `vector` extension available,
   and a schema-owner login. The owner must be allowed to create roles; the setup job
   creates the API's and the worker's logins from the passwords in your `.env`.
3. **A bucket** for uploaded documents: S3 or anything that speaks S3.
4. **Keys**, generated once and kept somewhere safe apart from the database:

   ```sh
   # SECRET_ENCRYPTION_KEYS: put the output inside {"k1": "..."}
   python -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"
   # STREAM_TOKEN_KEY
   python -c "import secrets;print(secrets.token_urlsafe(48))"
   # SESSION_ENCRYPTION_KEY
   python -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"
   ```

   `SECRET_ENCRYPTION_KEYS` encrypts every provider account saved in the product. If it
   is lost, the database backup cannot bring those accounts back: each has to be entered
   again. Never replace it; to rotate, add a second key (see below).
5. **Provider accounts** you will enter in the product afterwards: Twilio, Deepgram,
   ElevenLabs, the AI model (OpenRouter or Gemini), an email server, and for calendar
   sync a Google Cloud OAuth app and a Microsoft Entra app. Register
   `https://<website address>/api/calendar/callback` as the redirect address of both.

## First start

```sh
# Build the three images, once per release.
docker build -t frontdesk-api:1.0.0 --target runtime .          # in the backend repository
docker build -t frontdesk-worker:1.0.0 --target worker .        # in the backend repository
docker build -t frontdesk-web:1.0.0 .                           # in the frontend repository

cd deploy
cp env.example .env      # fill in every value
docker compose up -d --wait
```

Then:

1. Open `https://<website address>` and sign in with `SUPER_ADMIN_EMAIL` and its password.
2. Turn on two-step verification under My account.
3. Under Platform, Settings: save the email server and send yourself the test email,
   then save the phone, speech and AI accounts. Each is checked as it is saved.
4. Open the System health tab. Every line should be green within two minutes.
5. Create a clinic, assign it a number, and call it.

Remove `SUPER_ADMIN_PASSWORD` from `.env` once you have signed in. It is only read when
the account does not exist yet.

## Upgrading

```sh
# Build the new images under a new tag, change the three *_IMAGE values in .env, then:
cd deploy
docker compose up -d --wait
```

Take a database backup first. The setup job applies the database changes before the
API and the worker start. When the API is stopped it reports itself not ready, takes no
new calls and gives calls in progress up to five minutes to end (`app/serve.py`; the
container's stop grace period is 330 s). Still, upgrade at a quiet time.

If the new version does not come up, read `docker compose logs setup api` before
anything else. Going back to the old images is safe only when the release changed
nothing in the database that the old version still needs; when in doubt, restore the
backup you took and start the old images.

## Backups

Two things have to be backed up, and kept apart from each other:

- **The database.** Use your provider's scheduled backups and point-in-time recovery.
  For a copy of your own, on a machine that has the PostgreSQL 17 client tools and a
  checkout of the backend (the server images do not carry `pg_dump`):

  ```sh
  # MIGRATION_DATABASE_URL in the environment, pointing at the database
  uv run python -m app.maintenance.backup backup --directory ./backups
  uv run python -m app.maintenance.backup verify-restore \
      --backup ./backups/<file>.dump --target-database frontdesk_restore_check
  ```

  The second command restores into a separate, empty database and checks that it is
  complete and that row-level security came back with it. Do it at least once a
  quarter: a backup nobody has restored is not a backup.
- **The keys** in `.env`, above all `SECRET_ENCRYPTION_KEYS`.

Uploaded documents live in the bucket; turn on its versioning.

## Is it working?

- `https://<api address>/health/ready` answers 200 when the API can reach the database
  and Redis. Point an uptime check at it.
- **Platform, Settings, System health** shows every part in words. The same thresholds
  are worth alerting on:

  | What | Problem when |
  | --- | --- |
  | Background worker | It has not reported for 3 minutes. Messages, reminders and calendar sync have stopped. |
  | Messages to patients | Any has waited more than 5 minutes; or any failed in the last 24 hours (warning). |
  | Invitations and password emails | Any has waited more than 5 minutes. |
  | Knowledge documents, call reports | Any has waited more than 10 minutes. |
  | Calendar sync | Appointments are not reaching calendars, or a calendar has not been read for 30 minutes. |
  | The receptionist | More than one reply in ten failed in the last hour; or the first words take more than 2.5 seconds (warning). |
  | Text messages from patients | Any has waited more than 2 minutes for a reply. |

- **Logs** are one JSON object per line. Alert on `"level": "ERROR"`; the worker writes
  `queues_running_late` at that level whenever something has waited too long. Also
  worth counting: `assistant_turn_unavailable`, a warning from the API each time a
  reply could not be given. Logs never contain what was said, a prompt, a key or a
  provider's response.
- With `OTEL_EXPORTER_OTLP_ENDPOINT` set, traces and metrics go to your collector.

## When something is wrong

**Nobody can sign in as a platform administrator.** On the server:

```sh
docker compose run --rm -e RECOVERY_PASSWORD='a new long password' setup \
    python -m app.platform.recover admin@example.com
```

This sets a new password, turns two-step verification off and signs the account out
everywhere. It works only for an existing platform administrator and is recorded.

**Calls are not answered.** In order: is the clinic "Taking calls"? Does System health
show a problem? Under Platform, Settings, Phone numbers: is the number assigned to the
clinic? Press "Point here again" on it, which tells the phone service this server's
address once more. Then check the phone, speech and AI accounts are all "verified".
A call that could not be answered opens a ticket in the clinic, with the caller's number.

**The receptionist answers "I cannot help right now".** The AI account is failing. The
call page shows the reason on each reply (key refused, no credit left, too busy).
Fix the account under Platform, Settings; nothing needs restarting.

**Texts are not answered.** The clinic must be "Taking calls" and its number assigned.
If System health shows texts waiting, restart the API: unanswered threads are picked up
again within half a minute of it starting.

**Messages or reminders are not sent.** System health will show the worker as stopped
or messages as waiting. `docker compose logs worker --tail 100`, then
`docker compose restart worker`. Nothing is lost: what was waiting is sent, except
messages more than six hours overdue, which are dropped on purpose.

**Emails do not arrive.** Platform, Settings, Email: "Save and check", then "Send test
email". Invitations wait in their queue until the email server works.

**A provider's calendar stopped syncing.** The provider's Calendar tab says "needs
attention" when Google or Microsoft withdrew access. Connect it again from that tab.

## Rotating the encryption key

Add a second key and make it the active one; keep the old one so existing data can
still be read:

```
SECRET_ENCRYPTION_KEYS={"k1": "<old>", "k2": "<new>"}
SECRET_ENCRYPTION_ACTIVE_KEY=k2
```

Restart. New and re-saved accounts use `k2`. Remove `k1` only after every provider
account has been saved again.

## Before a release

Run these against the version you are about to deploy:

1. The automated checks (both repositories' CI): lint, types, tests, build, a fresh
   database set up twice, no drift between the models and the migrations.
2. **The receptionist against the real model.** This costs a few cents:

   ```sh
   EVAL_PROVIDER=openrouter EVAL_MODEL=google/gemini-3.8-flash EVAL_API_KEY=... \
       python -m tests.evals
   ```

   It needs a disposable database, as the integration tests do. It passes when nine
   in ten conversations are handled, every safety conversation is handled, and the
   first words of a reply take no more than 2.5 seconds as a median.
3. **One real call and one real text** to a test clinic: book an appointment on each,
   check the confirmation arrives and the appointment appears in the provider's calendar.

## Limits to know

- One API process holds a call from start to end; a stop waits up to five minutes for
  calls in progress, then ends them.
- Speech: Deepgram Flux for recognition (Nova 3 for Gujarati) and the ElevenLabs
  WebSocket for speech. Choose them under the platform's speech connections; a connection
  saved before this release keeps Nova 3 and Turbo 2.5 until it is changed.
- A transfer nobody answers can come back to the receptionist only within three minutes
  of the redirect; the call summary staff read covers the part before the transfer.
- The cap of eight replies at once per clinic, and the circuit breaker for an AI
  account, are per API process. Behind a load balancer with several API processes they
  apply to each separately.
- Changes in a provider's own calendar are picked up every five minutes, and once more
  just before a booking is saved.
