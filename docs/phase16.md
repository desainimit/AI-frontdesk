# Phase 16 — Post-call processing

The existing call-end transaction queues a `PostCallWorkflow`. The voice pipeline now keeps at most 80 redacted speaker turns and 16 KB of transcript text in ephemeral conversation state. Media cleanup saves a bounded snapshot in `call_transcripts`; the workflow reads that snapshot after the call ends. If a provider callback ends the call first, the workflow checks again for up to 30 seconds. It then writes one `post_call_reports` row and, when needed, one `follow_up_events` row. Retries cannot duplicate either row. The caller does not wait for this processing.

The summary is a short extract of observed caller and agent speech in its original language. The structured outcome records call status, resolution, language, intent, safety decision, and follow-up kind. This deterministic implementation does not guess a diagnosis, appointment outcome, or clinical facts from speech. It uses explicit conversation state and call status. Common verification codes, account identifiers, dates of birth, email addresses, and phone-like numbers are redacted before snapshots are stored; this is **targeted redaction, not full de-identification**. Treat summaries and snapshots as sensitive tenant data. They are protected by forced RLS and separate runtime/worker database grants.

`human_review` follow-ups are created for emergency/transfer escalation or failed/disconnected calls. `appointment_followup` is created when an appointment action was proposed but not completed. Follow-up rows form an operator work queue; no SMS, email, or CRM message is sent. Report views and follow-up acknowledgements are audited. Daily analytics use the call-end date from committed reports, so a delayed or retried workflow does not shift or inflate totals.

## Apply and inspect

Apply `0013_post_call` and reprovision the restricted roles before starting the updated API and workflow worker:

```powershell
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python.exe -m app.database.provision
```

The database still needs the Phase 11 `pgvector` extension; the default Compose file does not start a PostgreSQL image. Continue using your local PostgreSQL server and the external/local Temporal service configured in [Phase 15](phase15.md). The workflow worker starts with `docker compose --profile workflows up --build -d workflow-worker` when those services are ready.

Authenticated tenant admins can use:

| Endpoint | Result |
| --- | --- |
| `GET /api/v1/calls/{call_id}/post-call` | Redacted summary and structured outcome |
| `GET /api/v1/follow-ups?limit=50&offset=0` | Pending operator work |
| `POST /api/v1/follow-ups/{event_id}/acknowledge` | Idempotent acknowledgement |
| `GET /api/v1/analytics/calls?from=2026-10-01&to=2026-10-31` | Daily total, completed, and follow-up counts |

`call:read` permits reports, queue, and analytics reads. `follow_up:manage` permits acknowledgement. Tenant RLS applies to the snapshot, report, and follow-up tables. A missing media snapshot produces a metadata-only report after the 30-second wait. The snapshot table persists redacted text for later review; define and test a retention policy before production under Phase 17.

Focused checks: `.venv\Scripts\python.exe -m pytest tests/test_postcall_phase16.py tests/test_workflows_phase15.py -q`. The database integration test in `tests/integration/test_postcall.py` needs an opt-in disposable PostgreSQL instance with pgvector and separate migration/runtime/worker credentials.
