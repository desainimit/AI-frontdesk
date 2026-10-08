# Phase 15 — Durable workflows

Phase 15 adds an optional Temporal worker for four workflows. API transactions write a small, tenant-scoped `workflow_intents` row beside the domain change. The worker leases committed intents and starts Temporal workflows with stable IDs, so a failed handoff can be retried without starting a second workflow. Database and document work runs in activities; the live voice path makes no Temporal request.

| Workflow | Trigger | Current effect |
| --- | --- | --- |
| `DocumentIngestionWorkflow` | New document version | Drives the existing leased knowledge job to a terminal state. |
| `PostCallWorkflow` | First completed call outcome | Records limited call metadata in `workflow_results`. |
| `TenantOnboardingWorkflow` | Tenant creation | Records tenant configuration status in `workflow_results`. |
| `AppointmentReminderWorkflow` | Booking or rescheduling | Waits until one hour before start, rechecks appointment status and version, then records a due marker. |

The reminder workflow **does not send a message**. Phase 14 defines email and SMS ports, but no tenant-specific provider or recipient delivery path is configured. Phase 16 extends the post-call workflow with redacted transcript processing, reports, and follow-up events; see [Phase 16](phase16.md). Workflow results contain no document text, transcript, phone number, or patient identifier. A rescheduled appointment gets a new generation; an older reminder skips after its status/version check. Pending intents remain in PostgreSQL while Temporal is disabled and are dispatched when the worker is enabled.

## Run

Apply the new migration and provision the restricted runtime and worker database roles with the existing migration credentials:

```powershell
.venv\Scripts\alembic.exe upgrade head
.venv\Scripts\python.exe -m app.database.provision
```

The existing Phase 11 migration requires PostgreSQL with `pgvector`. You can use your local PostgreSQL server; there is no PostgreSQL service in the default Compose file. The Temporal server is also external to Compose. Start a local Temporal development server separately or use an existing Temporal namespace, then set `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, and, when required, `TEMPORAL_API_KEY` plus `TEMPORAL_TLS=true`. Keep API and worker database credentials separate. The worker requires `WORKER_DATABASE_URL`; document ingestion also requires the existing `KNOWLEDGE_ENABLED=true` setup and storage/embedding configuration from [Phase 11](phase11.md).

For a host process, install the optional workflow dependencies and run the worker:

```powershell
.venv\Scripts\uv.exe sync --locked --extra workflows --extra knowledge
$env:TEMPORAL_ENABLED = "true"
.venv\Scripts\python.exe -m app.workflows.worker --check
.venv\Scripts\python.exe -m app.workflows.worker
```

For Docker, set `TEMPORAL_ADDRESS=host.docker.internal:7233` when Temporal listens on the host (the `.env.example` value `localhost:7233` works only for a host process). Then run:

```powershell
docker compose --profile workflows up --build -d workflow-worker
docker compose --profile workflows logs -f workflow-worker
```

The profile builds the worker image locally and does not add a PostgreSQL or Temporal image. `--check` verifies the worker database role and Temporal connection; `--once` dispatches at most one pending intent. Inspect `workflow_intents` and `workflow_results` with a tenant-scoped database session, or use the Temporal UI for workflow state. Keep the legacy knowledge worker stopped if you want Temporal to drive document jobs exclusively; both workers use the same knowledge-job lease if run together.

Run the focused tests with `.venv\Scripts\python.exe -m pytest tests/test_workflows_phase15.py -q`. The tests use Temporal's time-skipping test server and need no live Temporal service. Database migration and role checks require a PostgreSQL instance with pgvector.
