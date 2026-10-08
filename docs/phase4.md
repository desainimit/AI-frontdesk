# Phase 4: reliability foundation

Phase 4 adds persistent request idempotency, a transactional outbox, versioned
appointment events, a background worker, typed provider failures, timeouts, and
bounded retries. Migration `0005_reliability` follows the platform-admin migration
`0004_platform_admin`; migration numbers are independent of build-plan phase numbers.

## Run

The project continues to use your installed PostgreSQL. `.env` now requires three
separate database logins, all pointing at that database:

| Variable | Used by | Access |
| --- | --- | --- |
| `MIGRATION_DATABASE_URL` | Migration/operator job | Schema and role management |
| `DATABASE_URL` | API | Tenant-scoped domain data, idempotency and outbox inserts |
| `WORKER_DATABASE_URL` | Worker | Outbox reads and delivery-state updates only |

Provisioning creates absent roles and preserves existing passwords. The worker
has no superuser or BYPASSRLS privilege, no clinical-table access, and cannot edit
event payloads. Its role-specific outbox policy permits processing across tenants.
The API cannot acknowledge deliveries or use the worker's credentials.

```powershell
docker compose up --build -d --wait
Invoke-RestMethod http://localhost:8000/health/ready
docker compose ps
docker compose logs -f worker
```

The migration job upgrades the schema and provisions both roles. Existing
super-admin bootstrap configuration remains in the migration job only. No
PostgreSQL image is pulled. To run a worker on the host, use host connection URLs,
set that process's `DATABASE_URL` to the worker URL, and run:

```powershell
uv run --locked python -m app.events.worker
# Process at most one eligible event and exit:
uv run --locked python -m app.events.worker --once
```

Worker settings: `OUTBOX_MAX_ATTEMPTS=5`, `OUTBOX_PROVIDER_TIMEOUT_SECONDS=10`,
`OUTBOX_LEASE_SECONDS=90`, and `OUTBOX_POLL_SECONDS=1`. The lease is bounded above
the maximum supported provider timeout. SIGTERM stops claiming work and lets the
current bounded attempt finish. The container health probe verifies worker database
access; it does not prove downstream delivery success.

## Idempotent appointment requests

Use a fresh `Idempotency-Key` for each logical booking, cancellation, or reschedule.
Retry with the same key **and the same body** after a timeout or uncertain response.
Keys accept 1–128 ASCII letters, digits, dots, colons, underscores, and hyphens.
Swagger exposes the optional header on all three appointment mutation endpoints.

```powershell
$headers = @{
    Authorization = "Bearer $tenantAdminToken"
    'Idempotency-Key' = [guid]::NewGuid().ToString()
}
$body = @{
    provider_id = $providerId
    patient_id = $patientId
    starts_at = $availableSlotStart
} | ConvertTo-Json
$uri = 'http://localhost:8000/api/v1/appointments'
$first = Invoke-RestMethod -Method Post $uri -Headers $headers `
    -ContentType 'application/json' -Body $body
$retry = Invoke-RestMethod -Method Post $uri -Headers $headers `
    -ContentType 'application/json' -Body $body
$first.id -eq $retry.id  # True; one appointment, one audit mutation, one event.
```

Use a tenant admin token, not a platform token. See the
[Phase 3 walkthrough](phase3.md) to create a provider, patient, and available slot.

- Keys are scoped to the authenticated tenant and actor. The request fingerprint
  includes the operation, target ID, and canonical validated command.
- Simultaneous identical requests serialize inside the database transaction and
  return the same saved response. A changed body/action with an already-used key
  returns `409 idempotency_key_reused`.
- Replays still require active tenant membership and current permissions. They
  return the original successful response snapshot, even if the appointment has
  since changed. Retrieve the appointment to obtain its current state.
- Failed transactions do not consume a key. A successful response, domain change,
  audit mutation, and outbox insert commit together. Records do not expire yet.
- The header remains optional for compatibility. Requests without it have normal
  booking-conflict protection but no saved-response replay guarantee. Idempotency
  is wired into appointment mutations; other creation endpoints do not yet accept
  this header. The reusable repository supports future workflows.

## Delivery behavior

Events are `appointment.created`, `appointment.rescheduled`, or
`appointment.cancelled`, with schema version 1 and an appointment snapshot. Payloads
contain identifiers, UTC times, status, and version; no names, phone numbers, bearer
tokens, or provider credentials. No event is emitted for a no-op or replay.
Existing appointments are not backfilled with historical events by the migration.

The worker commits a `FOR UPDATE SKIP LOCKED` claim before invoking a provider.
Claims have lease tokens and expiration times. A crashed worker's expired claim
can be reclaimed, and an old lease cannot acknowledge another worker's attempt.
Outstanding events for each appointment are handled in version order. A terminal
dead event stops blocking later versions; providers should also handle versions
when integrating stateful systems such as calendars.

| Result | Behavior |
| --- | --- |
| Success | Mark delivered and clear the lease |
| Temporary error / timeout | Persist retry with exponential backoff and jitter |
| Rate limit | Honor a bounded retry-after hint |
| Permanent error / unsupported event schema | Retain as `dead` for operator review |
| Retry limit reached | Retain as `dead`; appointment stays committed |
| Process crash / failed acknowledgment | Reclaim after lease expiration |

Delivery is **at least once**. The stable outbox event UUID is passed to the provider
as its idempotency key. Real adapters must enforce that key at the downstream
side-effect boundary. A crash after a provider accepts a request but before the
database acknowledgment can cause a repeat call; this design does not claim
universal exactly-once external delivery.

`NOTIFICATION_PROVIDER=console` is the currently supplied adapter. It logs
`notification_simulated`, with event/tenant IDs, and marks that simulated delivery
complete. It sends no SMS, email, or calendar request. Provider abstractions and
failure/retry behavior are tested with controlled adapters; external integrations
remain later phases.

Delivery outcomes and durations have OpenTelemetry metrics
`outbox.delivery.attempts` and `outbox.delivery.duration`. Logs and stored failure
codes omit provider exception messages. Operators can inspect the queue in their
database administration tool:

```sql
SELECT id, tenant_id, event_type, status, attempts, available_at,
       lease_until, delivered_at, last_error
FROM outbox_events
ORDER BY created_at DESC;
```

Dead events are retained, not silently discarded. Automated dead-letter replay,
operator dashboards, and retention cleanup are not implemented in this phase.

## Verification

The full integration suite needs a disposable PostgreSQL database with all three
URLs configured and provisioned, plus real Redis. Never point tests at an
application or production database; worker tests modify their disposable queue.

```powershell
$env:RUN_INTEGRATION = 'true'
uv run --locked alembic upgrade head
uv run --locked python -m app.database.provision
uv run --locked alembic check
uv run --locked pytest
```

Tests verify concurrent request replay, atomic rollback, tenant isolation, external
failure after appointment commit, typed retries/timeouts, dead-letter exhaustion,
concurrent claims, event ordering, lease recovery, and stable provider keys after
a delivery/acknowledgment crash. See [recorded results](validation.md).
