# Fresh database setup

Run `python -m app.setup` as the one-shot deployment job before
starting the API or the worker. It applies every migration, provisions and verifies
the restricted API and worker logins, and creates the first Super Admin from
operator-supplied credentials. The same command can run again without resetting
passwords or tenant settings. Compose and backend CI use this path; CI runs it twice
against an empty PostgreSQL service.

Required infrastructure configuration:

- `MIGRATION_DATABASE_URL`: schema owner, available only to the deployment job.
- `DATABASE_URL`: separate API login in the same database.
- `WORKER_DATABASE_URL`: separate worker login.
- `REDIS_URL`: the configured Redis or Valkey endpoint.
- `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD`, and optionally `SUPER_ADMIN_NAME`:
  bootstrap identity. There is no shared default password. Existing identities
  are never promoted, re-enabled or assigned a replacement password by rerunning setup.

Encryption keys, SMTP, public origins and provider connections are configured
through the documented deployment and integration paths. Initialization does not
invent secrets or mark connections verified.

## Persisted workspace defaults

New workspace creation atomically saves the tenant, pending owner membership, first
location, editable roles with explicit true/false permission records and the
configuration draft. No settings page has to be opened to initialize these records.
The tenant starts in `setup` and its owner accepts a single-use invitation.

Owner, Admin, Operator and Analyst are editable initial roles. Member is also seeded
for compatibility with existing membership operations. Permission names come from the
backend catalog; role names do not determine authorization.

The starting configuration is still minimal: English only, no opening hours, read-only
assistant tools and every channel disabled. Complete, ready-to-use defaults for a new
clinic are part of the rebuild plan and replace this section when they land.
