# Phase 9: appointment agent

The voice agent can identify a reception-authorized patient, list providers, read
available slots, and propose booking, cancellation, or rescheduling. The server
asks for explicit confirmation and calls the existing appointment domain services.
No new provider dependency is required.

## Setup

This phase adds migration `0007_appointment_agent`: call-bound patient access and
call-scoped idempotency. Apply it to your existing local PostgreSQL database:

```powershell
docker compose build api
docker compose run --rm --no-deps migrate
docker compose up -d api worker
```

Alternatively, in the configured local Python environment:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m app.database.provision
```

No PostgreSQL container is needed. Configure the existing Twilio, STT and TTS
settings, `TELEPHONY_MODE=pipeline`, and `AGENT_ENABLED=true`. Natural-language
appointment planning requires `LLM_PROVIDER=openai`, a configured `LLM_MODEL`, and
`OPENAI_API_KEY` in `.env`. The existing mock is a directory/intent demonstration;
appointment acceptance tests use a scripted provider and real domain services.
No live external provider calls were made during implementation.

## Patient access before Phase 13

Names, caller ID, and phone numbers do not authorize patient access. Automated
OTP/DOB/account verification remains Phase 13. This phase supplies a manual
reception bridge: an authenticated tenant admin verifies the patient through the
clinic's established process and explicitly attests to that verification.

For an active call, issue a short-lived access code using the tenant admin token:

```powershell
$body = @{
    patient_id = "<existing patient UUID>"
    verification_attested = $true
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
    -Uri "http://localhost:8000/api/v1/calls/$callId/patient-access" `
    -Headers @{ Authorization = "Bearer $tenantAdminToken" } `
    -ContentType "application/json" -Body $body
```

The response contains an eight-digit code and its expiry. Reception must convey
it to the verified caller through an established channel; this phase does not
send SMS or transfer the call. The caller provides the code to the agent.
A standalone eight-digit utterance is handled without contacting the LLM and is
redacted in stored conversation state. Do not include codes in application logs.
If embedded in a longer utterance, it may reach the configured LLM as caller text.

Codes are salted/scrypt-hashed, valid for five minutes, bound to one tenant,
patient and active call, and limited to three attempts. Reissuing does not reset
the attempt counter. Successful redemption consumes the code and binds that
patient for the remaining call. The binding cannot be switched to another patient.
Without this reception step, public directory/availability reads work, while
patient-specific reads and mutations remain blocked. New patient registration
through the voice agent is not included.

## Tools and deterministic flow

| Tool | Behavior |
| --- | --- |
| identify_patient | Redeem the reception-issued code; return status, not a patient record |
| get_providers | Read tenant provider directory |
| check_availability | Read up to 20 available slots in a range of at most 31 days |
| list_my_appointments | Read only the authorized patient's appointments |
| book_appointment | Validate and propose a booking |
| cancel_appointment | Check ownership/version and propose cancellation |
| reschedule_appointment | Check ownership/version/availability and propose a new time |

The earlier `list_locations` and `list_providers` tools remain available.
All arguments use strict schemas. Tenant, patient binding, confirmation, expected
version and idempotency keys are supplied by server code, never model arguments.
Dates must include a timezone offset; prompts direct the model to use the clinic
timezone and clarify ambiguous dates. Confirmation includes the provider and
actual timestamp with an offset.

Flow states cover collecting details, checking availability, offering slots,
awaiting confirmation, executing, completed and failed. A proposal performs no
appointment write. An unambiguous affirmative reply executes that exact proposal
without asking the LLM to interpret or repeat the action. Negative, ambiguous or
changed-time replies invalidate it; the replacement needs fresh confirmation.
Expired confirmations cannot execute. One mutation proposal is allowed per model
batch. Successful write responses come directly from the server's committed result.

Caller permissions are deliberately narrower than tenant admin permissions.
Each domain transaction rechecks the active call, tenant, phone binding, provider
call ID and patient binding. Patient ownership is enforced for appointment reads,
cancellation and rescheduling. Booking, database overlap protection, optimistic
versions, audit events and transactional outbox reuse the existing domain layer.
Caller domain audits use the system actor with the call ID; the reception
attestation separately records the authenticated tenant admin.

## Interruption and retries

Existing voice barge-in continues to cancel response/speech work and clear Twilio
playback. A confirmed operation keeps its server-generated idempotency key when
its response is interrupted. Retrying an uncertain operation reconciles through
the same domain request and stored result; it cannot create a second appointment.
Interruption does not undo a committed booking. A new cancellation or reschedule
requires its own explicit confirmation. Expired/closed calls cannot issue writes.

Call state is owned by one claimed media stream; this does not add cross-process
media resumption. Downgrading the migration removes call access state and call-only
idempotency responses because the previous schema cannot represent them.

## Validation and changed areas

- `app/agents/appointment_flow.py` and `appointment_tools.py`: proposals, typed tools,
  confirmation and interrupted-operation replay.
- `app/calls/access.py`, call models, and telephony routes: reception authorization
  and voice integration.
- Auth context/policies, unit of work, appointment repository/service and idempotency:
  restricted caller scope and ownership without granting an admin role.
- Conversation state/manager: explicit pending operation and access-code redaction.
- `migrations/versions/0007_appointment_agent.py`: reversible schema update.
- `tests/test_appointment_agent.py` and `tests/integration/test_appointment_agent.py`:
  validation, booking lifecycle, changed time, confirmation, slot conflict, ownership,
  code expiry/lockout, authenticated endpoint, concurrent replay, and an entire
  booking/confirmation turn through the media WebSocket using scripted providers.

Run the full database-backed suite using the disposable database instructions in
README; [validation.md](validation.md) records the observed checks. Live-call
acceptance still requires configured providers and an actual phone call.
