# Phase 3: domain layer

Phase 3 implements Location, Provider, Patient, Availability, and Appointment with
models, validated commands/response schemas, repositories, services, domain errors,
and authenticated REST endpoints. Services receive the Phase 2 request context
and use its tenant-scoped Unit of Work. No LLM or voice service is needed.

## Start or upgrade

Keep your existing `.env` and locally installed PostgreSQL. From the project folder:

```powershell
docker compose up --build -d --wait
Invoke-RestMethod http://localhost:8000/health/ready
```

The migration job applies `0003_domain`, provisions domain permissions, and checks
the restricted runtime login before the API starts. No PostgreSQL image is pulled.
The migration installs PostgreSQL's `btree_gist` extension, which must be available
to the migration owner. The runtime role must remain a different, non-owner login.
Host Python users should run `alembic upgrade head` followed by
`python -m app.database.provision` using the locked environment and host URLs as
described in [README](../README.md).

Open <http://localhost:8000/docs>, select **Authorize**, and paste your Phase 2
tenant admin token. If you need a tenant/token, follow
[Create a tenant and its first admin](phase2.md#create-a-tenant-and-its-first-admin).
No demo tenant or patient is automatically inserted into your application database.

## Endpoints

All paths below start with `/api/v1`. Lists of locations, providers, patients,
and appointments accept `limit` (1–100, default 50) and `offset` (default 0).

| Method / path | Purpose | Permission |
| --- | --- | --- |
| `POST /locations` | Create location with IANA timezone | Admin |
| `GET /locations` | List locations | Member/admin |
| `POST /providers` | Create provider belonging to one location | Admin |
| `GET /providers` | List providers | Member/admin |
| `POST /patients` | Create patient with name and E.164 phone | Admin |
| `GET /patients`, `GET /patients/{id}` | Read patients, audited | Admin |
| `POST /providers/{id}/availability` | Define dated availability window | Admin |
| `GET /providers/{id}/availability` | Read windows overlapping query | Member/admin |
| `GET /providers/{id}/slots` | Read available provider slots | Member/admin |
| `POST /appointments` | Book a slot | Admin |
| `GET /appointments`, `GET /appointments/{id}` | Read appointments, audited | Admin |
| `POST /appointments/{id}/cancel` | Cancel and release slot | Admin |
| `POST /appointments/{id}/reschedule` | Move within the same provider's availability | Admin |

Availability and slot reads require `starts_at` and `ends_at` query parameters.
Supply offset-aware ISO 8601 values, such as `2030-10-01T09:00:00+05:30`; URL-encode
the `+` when building query strings manually. Swagger does this for you.

## Try the full flow in PowerShell

Use a development tenant. This example creates synthetic records that remain in
that tenant, and leaves the example appointment cancelled. It does not require any
external integration.

```powershell
$base = 'http://localhost:8000/api/v1'
$secureToken = Read-Host 'Tenant admin token' -AsSecureString
$headers = @{
    Authorization = 'Bearer ' + ([System.Net.NetworkCredential]::new('', $secureToken)).Password
}

function Send-Frontdesk([string]$path, [hashtable]$body) {
    Invoke-RestMethod -Method Post -Uri "$base/$path" -Headers $headers `
        -ContentType 'application/json' -Body ($body | ConvertTo-Json)
}

$location = Send-Frontdesk 'locations' @{name='Demo clinic'; timezone='Asia/Kolkata'}
$provider = Send-Frontdesk 'providers' @{
    location_id=$location.id; display_name='Demo provider'; specialty='General'
}
$patient = Send-Frontdesk 'patients' @{
    display_name='Synthetic patient'; phone='+919999999999'
}
$start = [DateTimeOffset]::UtcNow.AddDays(2)
$end = $start.AddHours(2)
$window = Send-Frontdesk "providers/$($provider.id)/availability" @{
    starts_at=$start.ToString('o'); ends_at=$end.ToString('o'); slot_minutes=30
}
$query = 'starts_at=' + [uri]::EscapeDataString($start.ToString('o')) +
    '&ends_at=' + [uri]::EscapeDataString($end.ToString('o'))
$slotsUri = "$base/providers/$($provider.id)/slots?$query"
$slots = @(Invoke-RestMethod $slotsUri -Headers $headers)
$slots | Format-Table

$booking = @{
    provider_id=$provider.id; patient_id=$patient.id; starts_at=$slots[0].starts_at
}
$appointment = Send-Frontdesk 'appointments' $booking
$appointment

# Expected: HTTP 409 slot_unavailable, with no second appointment.
try { Send-Frontdesk 'appointments' $booking }
catch { Write-Host "Duplicate booking status: $([int]$_.Exception.Response.StatusCode)" }

$appointment = Send-Frontdesk "appointments/$($appointment.id)/reschedule" @{
    starts_at=$slots[1].starts_at; expected_version=$appointment.version
}
$appointment = Send-Frontdesk "appointments/$($appointment.id)/cancel" @{
    expected_version=$appointment.version
}
$appointment
Invoke-RestMethod $slotsUri -Headers $headers | Format-Table
Invoke-RestMethod "$base/audit-events?limit=100" -Headers $headers | Format-Table
Remove-Variable headers, secureToken
```

Expected results: four initial slots, booking version 1, duplicate booking 409,
rescheduled version 2, cancelled version 3, and all four slots available again.
Audit events include each successful creation, rescheduling, and cancellation.

## Scheduling and consistency rules

- Location timezones are validated IANA identifiers. All submitted times require
  explicit offsets and normalize to UTC. The timezone does not implicitly convert
  naive local times. `tzdata` supplies timezone data on Windows.
- Availability consists of future dated windows, at most 24 hours each, with fixed
  slots of 5–240 minutes. A window must fit at least one slot. Provider windows
  cannot overlap; use separate windows for breaks. Slot queries span at most
  31 days and return only complete, future slots inside the query.
- Bookings start exactly on the availability grid. Their duration comes from the
  window, not the caller. Half-open intervals allow adjacent appointments.
- PostgreSQL exclusion constraints reject overlapping active bookings for the
  same provider **or patient**, including concurrent requests. Provider slot
  listings do not account for a particular patient's bookings with other providers;
  the final booking enforces both constraints and may return 409.
- Cancellation and rescheduling lock the appointment and require its current
  `expected_version`. Successful mutations increment the version. A stale version
  returns `409 stale_appointment`. A failed reschedule leaves the old booking and
  version intact. Already-started appointments cannot be changed.
- Repeating cancellation of an already-cancelled appointment returns that record
  without another mutation or audit event. Rescheduling to the existing slot with
  the current version is also a no-op. Phase 4 now adds `Idempotency-Key` replay for
  appointment mutations; see [the reliability guide](phase4.md).
- All five domain tables have forced RLS and composite foreign keys for tenant
  associations. Request bodies reject supplied `tenant_id` fields; tenant identity
  comes from the verified bearer token. Cross-tenant references return 404.
- Every mutation and its audit record commit atomically. Patient and appointment
  reads are audited. Audit records contain identifiers, not names or phone numbers.

There are no recurring schedules, availability edits, provider transfers,
reminders, external calendars, or voice/AI endpoints in this phase. Phase 4 adds
the outbox worker separately.

## Automated verification

Run Ruff, Pyright, and pytest using the commands in [README](../README.md#checks).
The scheduling unit tests run without services. Integration tests require a
**disposable** PostgreSQL database with separate owner/runtime credentials,
applied migrations and runtime provisioning, plus `RUN_INTEGRATION=true` (or
`RUN_POSTGRES_INTEGRATION=true` to omit the real Redis readiness check).

`tests/integration/test_domain.py` covers the full HTTP lifecycle, concurrent
bookings, audit rollback, stale versions, time boundaries, cross-tenant references,
member permissions, direct SQL overlap constraints, and RLS queries without tenant
WHERE clauses. See [recorded validation results](validation.md).
