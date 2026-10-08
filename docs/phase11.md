# Phase 11 — Tenant knowledge base

Phase 11 adds versioned static documents and source-aware retrieval. Tenant admins upload PDF, DOCX, or Markdown files; a separate worker parses them with Docling, creates embeddings, and stores chunks in PostgreSQL with pgvector. A document becomes searchable only after an admin approves a ready version. Replacing an approved document immediately returns it to draft, so old chunks cannot answer new questions. Archiving removes it from retrieval.

## Prerequisites

Install [pgvector](https://github.com/pgvector/pgvector#installation) into your existing PostgreSQL 17 installation **before** running the migration. The Python `pgvector` package is only the database client; it does not install the server extension. The migration creates the extension in the project database and will fail if `vector.control` is absent. pgvector's [Windows build instructions](https://github.com/pgvector/pgvector#windows) use the Visual Studio x64 Native Tools Command Prompt, `PGROOT` set to your PostgreSQL 17 directory, then `nmake /F Makefile.win` and `nmake /F Makefile.win install`. Run the installation with the privileges required by your PostgreSQL installation. No PostgreSQL Docker image is needed.

The worker needs the `knowledge` Python extra (`uv sync --locked --extra knowledge`) when running on the host. It includes Docling and its parsing models. The regular API image remains smaller; Compose builds a separate worker image for this extra.

Set in `.env`:

```dotenv
KNOWLEDGE_ENABLED=true
OPENAI_API_KEY=your-existing-api-key
KNOWLEDGE_STORAGE_BACKEND=local
```

The knowledge system uses OpenAI `text-embedding-3-small` with 1536 dimensions for indexing and queries ([official OpenAI embeddings guide](https://developers.openai.com/api/docs/guides/embeddings)). The same `OPENAI_API_KEY` can serve the appointment agent when that agent is configured. Provider calls may incur cost.

Local object storage uses `.local/knowledge-objects` for host Python. In Compose, the API and knowledge worker share a persistent `knowledge-objects` volume. For staging/production, configure `KNOWLEDGE_STORAGE_BACKEND=s3`, `KNOWLEDGE_S3_BUCKET`, and region or endpoint as needed. The S3 client can use an IAM role or standard AWS credential environment variables. Only generated object keys are stored; filenames never become filesystem paths. Production settings reject local storage. Keep object storage private.

Run the migration and start both API and worker:

```powershell
docker compose run --rm --build migrate
docker compose --profile knowledge up --build -d api knowledge-worker
```

For host Python with local PostgreSQL and a separate `WORKER_DATABASE_URL`:

```powershell
.\.venv\Scripts\python.exe -m uv sync --locked --extra knowledge
.\.venv\Scripts\python.exe -m app.database.initialize
.\.venv\Scripts\python.exe -m app.knowledge.cli
```

The migration creates document, version, chunk, and job tables with forced tenant row security. The API role is tenant scoped; the existing restricted worker login can claim scheduling IDs from `knowledge_jobs`, then sets tenant scope before reading files' database records or writing chunks. The API's vector query includes the tenant predicate before similarity ordering. [Docling's document converter](https://docling-project.github.io/docling/reference/document_converter/) and [hierarchical chunker](https://docling-project.github.io/docling/concepts/chunking/) preserve headings and page provenance. Uploads are capped at 10 MB by default; PDF parsing is capped at 100 pages. Worker jobs retry and eventually show `failed` without exposing provider errors.

## Use the API

Send the raw file bytes with a tenant admin bearer token and `X-File-Name`, such as `hours.md`:

```powershell
$headers = @{ Authorization = "Bearer $token"; 'X-File-Name' = 'hours.md' }
Invoke-RestMethod -Method Post -Uri 'http://localhost:8000/api/v1/knowledge/documents' -Headers $headers -ContentType 'text/markdown' -InFile 'hours.md'
```

The response has document status `draft`. Poll `GET /api/v1/knowledge/documents/{id}/versions` until the current version is `ready`, then `POST /api/v1/knowledge/documents/{id}/approve`. Upload a replacement with `POST /api/v1/knowledge/documents/{id}/versions`; it needs a fresh approval. `POST /api/v1/knowledge/documents/{id}/archive` removes all versions from search. List documents with `GET /api/v1/knowledge/documents` and query approved sources with `POST /api/v1/knowledge/search`:

```json
{"question": "Where can I park?", "limit": 3}
```

Search returns document ID/name, version, heading, page number, excerpt, and distance. Tenant members can read/search; only admins can upload, approve, replace, and archive. Those writes are audited. The agent exposes `get_knowledge` only when knowledge is enabled, and gives source metadata to the model. If retrieval finds no approved source, the server gives a fixed response instead of letting the model invent one. Documents remain untrusted data and cannot change tool permissions. Live availability, appointments, patient records, payments, and schedules use their authoritative tools, not document retrieval.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_knowledge_phase11.py tests/test_knowledge_agent.py -q
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\pyright.exe
```

The integration test `tests/integration/test_knowledge.py` uses real PostgreSQL, pgvector, the restricted worker login, and Docling. It checks tenant isolation, version replacement, approval, and archival. Run it only in a disposable database with the repository's integration test environment. Mocked embeddings keep that test offline; live parsing quality, provider latency, and real caller behavior still need acceptance calls and documents for each tenant.
