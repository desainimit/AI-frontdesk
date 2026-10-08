# Phase 8: agent runtime

This records the initial read-only runtime. [Phase 9](phase9.md) extends it with
patient-authorized appointment tools and server-enforced confirmation.

Final STT utterances now optionally pass through an agent before TTS. The runtime
identifies intent, reads structured conversation state, requests registered tools,
and produces a bounded spoken reply. The existing echo/STT-repeat modes remain
available with the agent disabled.

## Implemented boundaries

- `LLMProvider` exposes text and Pydantic structured generation. Vendor SDK imports
  live in the OpenAI adapter. A deterministic mock supports offline tests.
- Immutable `AgentContext` comes from the authenticated, claimed Twilio call.
  Neither caller text nor model arguments can supply tenant scope or permissions.
- Prompt order is system policy, server-controlled tool descriptions, then user
  data containing a minimal state snapshot, utterance, and tool results.
  Identity secrets, caller phones, and full transcript history are excluded.
- Strict decision and argument validation rejects unknown tools, extra fields,
  coercions, duplicate JSON keys, out-of-range pagination, and invalid output.
  Every requested batch is validated before any handler runs.
- Registry handlers are thin adapters to `CallDirectoryService`. The service
  rechecks the active call, provider call ID, active tenant and phone binding
  inside a tenant-scoped PostgreSQL transaction. Successful reads are audited.
- Call safety decisions and completed calls block agent execution. Tool calls,
  model steps, response size, per-tool time, and total turn time are bounded.
  Provider failures/refusals and invalid output produce a static fallback;
  prompts, tool arguments, and provider error bodies are not logged.

Only `list_locations` and `list_providers` are registered. Their arguments are
`limit` (1?20, default 5) and `offset` (0?1000, default 0).
They expose clinic directory metadata, never patient records or appointments.
Booking, cancellation, rescheduling, identity verification, and call transfers
are not executable tools in this phase.

## Enable locally

For offline model behavior:

```dotenv
AGENT_ENABLED=true
LLM_PROVIDER=mock
TELEPHONY_MODE=pipeline
```

The mock is a deterministic demonstration, not a real language model, and is
rejected in staging/production. Mock STT/TTS can also be selected for automated
tests, but do not recognize or synthesize real caller speech.

For real model behavior, set these in your local `.env`:

```dotenv
AGENT_ENABLED=true
LLM_PROVIDER=openai
OPENAI_API_KEY=<your key>
LLM_MODEL=<your Responses and Structured Outputs capable model>
TELEPHONY_MODE=pipeline
```

The adapter uses Responses structured output parsing with `store=false`,
no automatic retries, and explicit output/time limits.
See the [official Structured Outputs documentation](https://developers.openai.com/api/docs/guides/structured-outputs).
Choose a model available to your account; no paid requests are needed to run tests.

Optional defaults:

```dotenv
LLM_TIMEOUT_SECONDS=10
LLM_MAX_OUTPUT_TOKENS=800
AGENT_MAX_STEPS=3
AGENT_MAX_TOOL_CALLS=4
AGENT_TURN_TIMEOUT_SECONDS=20
AGENT_TOOL_TIMEOUT_SECONDS=3
```

Twilio, STT and TTS credentials from Phase 5/6 must also be configured for a real
phone conversation. The API alone receives voice and model credentials through
Compose; migration and outbox worker services do not receive them.

```powershell
docker compose up -d --build
```

Use your installed PostgreSQL. Phase 8 introduces no new schema migration;
the schema head remains `0006_telephony`.

## Check it

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_agent_runtime.py tests/test_llm_provider.py tests/test_conversation_regressions.py tests/test_voice_phase6.py
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pyright.exe
```

Integration tests in `tests/integration/test_agents.py` exercise real PostgreSQL
call authorization, cross-tenant denial, tool audit records, and Redis TTL/isolation.
Follow [validation instructions](validation.md) for disposable database setup.

For live acceptance, call your configured Twilio number and ask ?Which doctors
are at this clinic?? or ?What clinic locations do you have?? Check that the answer
matches the tenant directory and that its audit trail contains
`agent_directory_providers_read` or `agent_directory_locations_read`.
A booking request should explain that reception must handle it.
Live provider credentials and an actual phone call are a separate verification step.
