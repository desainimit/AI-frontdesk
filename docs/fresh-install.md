# Fresh database initialization

Run `python -m app.database.initialize` as the one-shot deployment job before
starting the API or workers. It applies every migration, provisions and verifies
the restricted API/worker logins, provisions the separate financial login when
configured, and creates the first Super Admin from operator-supplied credentials.
The same command can run again without resetting passwords or tenant settings.
Compose and backend CI use this path; CI runs it twice against an empty PostgreSQL service.

Required infrastructure configuration:

- `MIGRATION_DATABASE_URL`: schema owner, available only to the deployment job.
- `DATABASE_URL`: separate API login in the same database.
- `WORKER_DATABASE_URL`: separate delivery-worker login.
- `FINANCIAL_DATABASE_URL`: separate financial login to enable financial commands.
  An unset value keeps financial commands unavailable. The API receives it for
  its isolated financial connection pool; delivery workers do not receive it.
- `REDIS_URL`: the configured Valkey endpoint.
- `SUPER_ADMIN_EMAIL`, `SUPER_ADMIN_PASSWORD`, and optionally `SUPER_ADMIN_NAME`:
  bootstrap identity. There is no shared default password. Existing identities
  are never promoted, re-enabled or assigned a replacement password by rerunning setup.

Encryption keys, SMTP, public origins and provider connections are configured
through the documented deployment and integration paths. Initialization does not
invent secrets, mark connections verified, create funds or publish commercial prices.
After sign-in and MFA enrollment, Super Admin publishes actual subscription prices
and creates a workspace through the normal onboarding flow.

## Persisted workspace defaults

New SaaS workspace creation atomically saves the tenant, pending owner membership,
first location, editable roles with explicit true/false permission records,
configuration draft, relational hours policy and setup progress. No settings page
has to be opened to initialize these records. The tenant starts in `setup` and its
owner accepts a single-use invitation before activation can succeed.

Defaults version 2 is copied into each new configuration:

| Setting | Starting value |
|---|---|
| First location | Main location; editable before creation |
| Time zone | Asia/Kolkata; selected location overrides it during onboarding |
| Language | English only; additional supported languages require selection |
| Agent actions | Registered safe read tools; no appointment mutation tools |
| Call duration preference | 20 minutes |
| Outbound quiet hours | 20:00–08:00 |
| Automatic attempts | Maximum 3 per destination per 24 hours |
| Transcript retention | 90 days |
| Consented audio retention | 30 days |
| Recording | Disabled |
| Auto-recharge | Disabled |
| Metered monthly budget | 0; the administrator must authorize a positive spending budget |
| Voice, chat, scheduling, SMS, email and workflows | Disabled pending configuration and readiness |
| Business hours | Empty until the administrator supplies actual hours |
| Published configuration | None; draft review and publication are explicit actions |

Owner, Admin, Operator, Analyst and Billing are editable initial roles. Member is
also seeded for compatibility with existing membership operations. Permission
names come from the backend catalog; role names do not determine authorization.

The new-workspace form reads its initial location, time zone and language policy
from the authenticated backend defaults endpoint. Configuration catalogs also
expose the versioned initial document. Every call to the defaults factory creates
an independent object. Existing drafts are returned unchanged by the seed helper;
new defaults never overlay saved tenant behavior.

## Verification and remaining boundary

An isolated empty PostgreSQL database was migrated from base through
`0036_usage_accounting_guards`. Initialization ran twice. Integration checks verify
metadata, restricted connections, persisted defaults before first read, idempotent
creation, unchanged edited settings, tenant isolation and financial accounting.

These checks establish fresh-install behavior for the implemented services.
Provider dispatch, self-service signup, payments and the remaining suite still
require implementation and their release gates. A fresh database is not evidence
that live calls, reminders or payment capture are ready.
