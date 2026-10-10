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

Encryption keys and public origins come from the deployment environment. Provider
accounts (phone, speech, AI model, email) are saved afterwards by a platform
administrator in the product; setup does not invent secrets or mark anything verified.
The migrations create the single `platform_settings` row with the product starting
values.

## What a new clinic starts with

Creating a clinic saves, in one transaction: the workspace, the pending owner, the first
location, four roles, its settings and two appointment types. Nothing has to be opened
or published before it works. The details are in [clinic settings and default
data](settings.md).
