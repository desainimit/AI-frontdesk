# Phase 5: telephony foundation

Phase 5 adds inbound telephony infrastructure: Twilio webhook processing, HMAC
request and stream signature verification, bidirectional Twilio Media Streams over
WebSocket, tenant phone routing, call lifecycle persistence (`CallContext`), and a
low-latency audio echo pipeline powered by Pipecat. Migration `0006_telephony`
follows the reliability migration `0005_reliability`.

## Run

The project continues to use your locally installed PostgreSQL. When telephony is
active, `.env` requires Twilio account credentials and an HTTPS public base URL:

| Variable | Required when | Format / Purpose |
| --- | --- | --- |
| `TELEPHONY_ENABLED` | Always | `true` to enable webhook routing and stream endpoints; `false` by default |
| `TWILIO_ACCOUNT_SID` | `TELEPHONY_ENABLED=true` | Twilio Account SID (`AC` followed by 32 hex characters) |
| `TWILIO_AUTH_TOKEN` | `TELEPHONY_ENABLED=true` | Secret auth token used for webhook signing and stream binding HMACs |
| `TELEPHONY_PUBLIC_BASE_URL` | `TELEPHONY_ENABLED=true` | Public HTTPS origin (e.g., `https://example.ngrok-free.app`), no trailing slash |
| `TELEPHONY_IDLE_SECONDS` | Optional | Transport inactivity timeout before disconnecting (default: 30s) |
| `TELEPHONY_MAX_CALL_SECONDS` | Optional | Maximum call duration before forced termination (default: 600s / 10m) |

```powershell
docker compose up --build -d --wait
Invoke-RestMethod http://localhost:8000/health/ready
docker compose ps
```

The migration job applies `0006_telephony` and provisions the restricted runtime
role (`frontdesk_app`) with least-privilege `SELECT, INSERT, UPDATE` on
`phone_numbers` and `calls`. The outbox worker role has no access to telephony
tables. For local host execution without Docker:

```powershell
$env:MIGRATION_DATABASE_URL = 'postgresql+asyncpg://postgres:<owner-password>@localhost:5432/frontdesk'
$env:DATABASE_URL = 'postgresql+asyncpg://frontdesk_app:<password>@localhost:5432/frontdesk'
$env:WORKER_DATABASE_URL = 'postgresql+asyncpg://frontdesk_worker:<password>@localhost:5432/frontdesk'
alembic upgrade head
python -m app.database.provision
```

## Telephony architecture & audio echo

```text
Caller
  │ (PSTN voice)
  ▼
Twilio Voice
  │ POST /webhooks/v1/twilio/incoming (validated signature & account SID)
  ▼
AI Frontdesk Webhook Handler
  │ Resolves tenant by dialed number via RLS (inbound_phone_lookup policy)
  │ Records Call (status="received") and emits audit event
  ▼
TwiML Response
  │ <Connect><Stream url="wss://.../media"><Parameter name="token" value="<hmac>"/></Stream></Connect>
  ▼
Twilio Media Stream
  │ WSS /webhooks/v1/twilio/{tenant_id}/{call_id}/media
  ▼
WebSocket Handler
  │ Validates signature, account SID, and HMAC stream token
  │ Moves Call state to "active"
  ▼
Pipecat Pipeline (EchoSession)
  │ 8000Hz 8-bit µ-law payload -> audioop.ulaw2lin -> PCM16 (8000Hz, mono)
  │ InputAudioRawFrame -> EchoProcessor -> AudioSink -> OutputAudioRawFrame
  │ PCM16 -> audioop.lin2ulaw -> 8-bit µ-law base64 JSON payload
  ▼
Twilio -> Caller hears their own audio echoed back
```

### Media stream security

1. **Webhook signing**: Every inbound HTTP webhook (`/incoming`, `/status`) and the
   initial WebSocket upgrade request is validated against `X-Twilio-Signature` using
   Twilio's standard HMAC algorithm over the full public URL and sorted form parameters.
2. **Stream token**: TwiML generates an HMAC-SHA256 token binding
   `frontdesk-stream-v1:{tenant_id}:{call_id}:{provider_call_id}` signed with the
   `TWILIO_AUTH_TOKEN`. When the WebSocket opens, the `start` event's custom parameter
   is verified in constant time. Unauthorized connections are rejected immediately
   with close code 1008.
3. **Tenant isolation**: Telephony tables enforce PostgreSQL Row Level Security (RLS).
   Phone numbers can only be assigned by platform super admins (`platform_phone_management`
   policy). Inbound lookups match only the specific incoming dialed number
   (`inbound_phone_lookup` policy). Calls are strictly partitioned per tenant
   (`tenant_isolation` policy).

## Endpoints

### Platform administration

| Method / path | Purpose | Permission |
| --- | --- | --- |
| `POST /api/v1/platform/phone-numbers` | Assign an E.164 number to an active tenant | Super admin |
| `GET /api/v1/platform/phone-numbers` | List assigned numbers | Super admin |

### Tenant call inspection

| Method / path | Purpose | Permission |
| --- | --- | --- |
| `GET /api/v1/calls` | List call history (audited `calls_viewed`) | Tenant admin |
| `GET /api/v1/calls/{call_id}` | Read single call details | Tenant admin |

### Twilio webhooks & media stream

| Method / path | Protocol | Purpose |
| --- | --- | --- |
| `POST /webhooks/v1/twilio/incoming` | HTTP | Inbound voice webhook; returns TwiML with `<Connect><Stream>` |
| `POST /webhooks/v1/twilio/{tenant_id}/{call_id}/status` | HTTP | Stream status callback (`stream-stopped`, `stream-error`) |
| `POST /webhooks/v1/twilio/status` | HTTP | Call status callback (`completed`, `busy`, `failed`, etc.) |
| `GET /webhooks/v1/twilio/{tenant_id}/{call_id}/media` | WebSocket | Bidirectional Twilio Media Streams audio echo |

## Call lifecycle & states

Calls transition through a strict state machine:

| Status | Description | Trigger |
| --- | --- | --- |
| `received` | Initial call logged from incoming webhook | Twilio HTTP `/incoming` webhook accepted |
| `active` | Media stream connected and validated | WebSocket `start` event validated and pipeline started |
| `completed` | Normal call termination | Stream `stop` event or Twilio `completed` callback |
| `disconnected` | Socket closed or idle timeout | Client disconnect or `telephony_idle_seconds` without messages |
| `failed` | Invalid stream format or pipeline exception | Format mismatch, HMAC failure, or unhandled exception |

Terminal states (`completed`, `disconnected`, `failed`) persist the termination
timestamp (`ended_at`), the termination reason (`end_reason`), and the frame
counters (`received_frames`, `sent_frames`). The first terminal outcome is preserved
even if the WebSocket close and Twilio status callback race.

## Local testing with Twilio & ngrok

### 1. Assign a phone number to a tenant

First, log in as platform super admin and assign your Twilio phone number (in E.164 format)
to your tenant:

```powershell
$adminToken = "..."  # From /api/v1/platform/auth/login
$tenantId = "..."    # Target tenant UUID

$body = @{
    tenant_id = $tenantId
    number = "+15551234567"  # Your Twilio phone number
} | ConvertTo-Json

Invoke-RestMethod -Method Post "http://localhost:8000/api/v1/platform/phone-numbers" `
    -Headers @{ Authorization = "Bearer $adminToken" } `
    -ContentType "application/json" -Body $body
```

### 2. Expose the local backend via HTTPS

Start ngrok (or your preferred tunnel) to forward to port 8000:

```powershell
ngrok http 8000
```

Set the public base URL in your `.env` and restart the API:

```text
TELEPHONY_ENABLED=true
TELEPHONY_MODE=echo
TWILIO_ACCOUNT_SID=ACXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
TWILIO_AUTH_TOKEN=your_auth_token_here
TELEPHONY_PUBLIC_BASE_URL=https://your-tunnel-subdomain.ngrok-free.app
```

### 3. Configure Twilio console

1. Go to **Phone Numbers** -> **Manage** -> **Active Numbers** in the Twilio Console.
2. Under **Voice Configuration**, set **A CALL COMES IN** to **Webhook**.
3. Enter URL: `https://your-tunnel-subdomain.ngrok-free.app/webhooks/v1/twilio/incoming`.
4. Set HTTP method to **HTTP POST**.
5. Configure the call status callback to POST to
   `https://your-tunnel-subdomain.ngrok-free.app/webhooks/v1/twilio/status`
   for terminal call events. The stream status callback is generated in TwiML.
6. Save changes.

### 4. Place a test call

1. Dial your Twilio phone number from any mobile or landline phone.
2. Twilio connects to `/webhooks/v1/twilio/incoming`.
3. You will hear: *"This is an audio echo test. Please speak now."*
4. Speak into the phone: your voice is captured, sent over WebSocket as $\mu$-law frames,
   routed through the Pipecat echo pipeline, and echoed back in real-time.
5. Hang up the call.
6. Inspect the call record using your tenant admin token:

```powershell
Invoke-RestMethod "http://localhost:8000/api/v1/calls" `
    -Headers @{ Authorization = "Bearer $tenantAdminToken" }
```

Response snapshot:
```json
[
  {
    "id": "e2c3491e-0897-4228-9844-01309fbcfddc",
    "tenant_id": "...",
    "phone_number_id": "...",
    "provider_call_id": "CA...",
    "stream_sid": "MZ...",
    "status": "completed",
    "created_at": "2026-09-27T21:00:00Z",
    "started_at": "2026-09-27T21:00:02Z",
    "ended_at": "2026-09-27T21:00:25Z",
    "received_frames": 1150,
    "sent_frames": 1150,
    "end_reason": "stream_stopped"
  }
]
```

The echo walkthrough explicitly uses `TELEPHONY_MODE=echo`; the default is
`pipeline`, which uses the Phase 6 providers and a different greeting. Media input
is validated as mono mu-law at 8 kHz and bounded before decoding. The idle timeout
measures missing transport messages, not acoustic silence: Twilio can continue
sending silent audio frames.

## Verification

Unit tests mock Twilio signatures and test the audio codecs, message parsing,
TwiML generation, and the real Pipecat `EchoSession` pipeline in memory:

```powershell
& .venv\Scripts\pytest.exe tests/test_telephony.py
```

The integration suite tests HTTP lifecycle, RLS tenant isolation, cross-tenant
rejection, WebSocket media streaming, frame counting, and disconnect cleanup on
a disposable PostgreSQL database:

```powershell
$env:RUN_INTEGRATION = 'true'
& .venv\Scripts\pytest.exe tests/integration/test_telephony.py
```
