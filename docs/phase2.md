# Phase 2: multi-tenancy

## Implemented boundaries

- `Tenant` is the root identity: its `id` is the RLS boundary. `User`,
  `ApiCredential`, and `AuditEvent` each have a non-null `tenant_id`.
- Application repositories filter by tenant, and PostgreSQL independently enables
  and forces RLS with both `USING` and `WITH CHECK` on all four tables. Missing or
  reset tenant scope yields no rows and rejects inserts. Composite foreign keys
  prevent a credential or audit actor from referring to another tenant's user.
- An immutable `RequestContext` carries request ID, tenant ID, actor ID/type, role,
  and an optional future call ID. Services receive it explicitly.
- `UnitOfWork.transaction(context)` creates a fresh async session/transaction,
  checks the database role, uses parameterized transaction-local `set_config`,
  rechecks active tenant/user membership and role, then commits or rolls back.
  Tenant scope does not survive commit/rollback or leak through the connection pool.
- Audit records include tenant, actor, action, resource IDs, request ID, optional
  call ID, and timestamp. Changes and their audit records share a transaction.
  Runtime privileges allow only audit SELECT/INSERT. Database triggers also
  reject UPDATE, DELETE, and TRUNCATE, including ordinary owner-issued commands.
  A privileged database administrator can still alter/drop triggers or tables;
  immutable external archival/retention controls remain later production work.

## Authentication and authorization foundation

Phase 2 uses opaque operator-issued bearer credentials, not an interactive login
system. Tokens have the form `fd1.<tenant UUID>.<credential UUID>.<random secret>`.
The secret contains 256 random bits. Only a SHA-256 digest of the whole token is
stored. The tenant/credential IDs are untrusted lookup hints until constant-time
digest verification succeeds. A narrowly scoped pre-auth transaction can read
only credential identity; domain operations receive context only after validation.

Credentials expire (30 days by default) and can be revoked. Disabled users or
tenants cannot authenticate. Roles come from the database, never headers or token
claims. An optional `X-Tenant-ID` header must match the verified tenant; it cannot
select another tenant. Missing/invalid credentials return 401 and a Bearer challenge;
missing permissions or mismatched tenant headers return 403.

| Endpoint | Member | Admin |
| --- | --- | --- |
| `GET /api/v1/tenants/current` | Allowed | Allowed |
| `GET /api/v1/users/me` | Own profile, audited | Own profile, audited |
| `GET /api/v1/users?limit=50&offset=0` | Denied | Current tenant only, audited |
| `GET /api/v1/audit-events?limit=50&offset=0` | Denied | Current tenant only, audited |

Responses never expose credential digests. Pagination is bounded. Tenant API
responses disable caching. There are no public tenant bootstrap endpoints,
passwords, sessions, OIDC, or user-management mutation endpoints yet. The API key
approach is a usable authentication foundation for this phase; identity-provider
integration can replace it behind `AuthenticationService` later. Use TLS outside
loopback development. Do not send credentials in URLs or log them.

## Database roles and setup

`DATABASE_URL` must use a dedicated non-owner login with no superuser, BYPASSRLS,
or role-creation privilege. Health checks and tenant transactions fail closed
with unsafe roles, missing RLS, or ownership of the tenant tables. PostgreSQL
superusers bypass RLS even when FORCE is set.

`MIGRATION_DATABASE_URL` is the schema owner/operator connection. It must be
different from the runtime login and have permission to create the restricted
login. It is passed only to the migration container. The API container receives
only runtime credentials. Keep both URLs in local `.env` or managed secrets.

The one-shot migration job runs `python -m app.database.initialize`, which applies
Alembic migrations and provisions the runtime login. The provisioner grants
SELECT on the four Phase 2 tables and INSERT on audit events. Phase 3 adds
SELECT/INSERT on its five domain tables and UPDATE on appointments.
It does not rotate existing role passwords or
silently downgrade an existing privileged role. Use a dedicated database/role.

If provisioning reports that the two URLs must use different PostgreSQL users,
keep the schema owner (for example, `postgres`) in `MIGRATION_DATABASE_URL` and
restore the restricted login and its own password in `DATABASE_URL`. Do not copy
the owner connection into both variables. Provisioning never resets an existing
role's password; changing the password text in `.env` alone does not change PostgreSQL.

For host Python, use `localhost` in both connection URLs, then:

```powershell
uv run --locked alembic upgrade head
uv run --locked python -m app.database.provision
uv run --locked uvicorn app.main:create_app --factory --reload --no-access-log
```

For containers on Docker Desktop, use `host.docker.internal` for your installed
PostgreSQL and run `docker compose up --build -d --wait`. No local PostgreSQL image
is pulled. Hosted CI uses its explicit disposable database overlay.

## Create a tenant and its first admin

This is an operator action using migration credentials. Choose a stable tenant
UUID; reusing it fails without creating duplicate data or issuing another token.
Tenant, admin, credential, and three audit records are inserted atomically.

```powershell
$tenantId = [guid]::NewGuid().ToString()
docker compose run --rm --no-deps migrate python -m app.tenants.cli create `
  --tenant-id $tenantId --name "My clinic" `
  --email "admin@example.com" --display-name "Clinic admin"
```

The operator receives the credential once on stdout. Store it securely. No real
tenant or bearer credential is created automatically during deployment.

```powershell
$token = Read-Host 'Bearer token' -MaskInput
Invoke-RestMethod http://localhost:8000/api/v1/tenants/current `
  -Headers @{ Authorization = "Bearer $token" }
```

`-MaskInput` requires PowerShell 7; on older PowerShell use a secure-input mechanism
appropriate to your environment. Revoke a credential using the IDs returned at creation:

```powershell
docker compose run --rm --no-deps migrate python -m app.tenants.cli revoke `
  --tenant-id $tenantId --credential-id YOUR_CREDENTIAL_UUID
```

Revocation is idempotent and audited once. There is no token refresh/rotation UI
in this phase. Operator tooling and identity-provider lifecycle workflows can be
extended when required; never use migration credentials in a request handler.

## Isolation test gate

Set `RUN_INTEGRATION=true` with a disposable migrated database, separate owner and
runtime URLs, and Redis. Run provisioning before pytest. The suite creates Tenant A
and Tenant B with users, credentials, and audit events, connects through the real
restricted login, sets Tenant A scope, and runs `select(User)` and equivalent
queries on all other tables **without a tenant WHERE clause**. Only Tenant A rows
are returned. Tests also cover missing scope, pool reuse, rollback, concurrent
contexts, cross-tenant inserts/foreign keys, credential forgery/expiry/revocation,
inactive identities, HTTP permissions, and durable append-only audit behavior.

RLS here protects against accidentally missing tenant filters. It does not make
arbitrary SQL execution safe: a compromised runtime connection can set a different
tenant variable. Untrusted callers/LLMs must never receive SQL access, credentials,
or direct control of the verified request context.

References: [PostgreSQL RLS](https://www.postgresql.org/docs/17/ddl-rowsecurity.html),
[transaction-local settings](https://www.postgresql.org/docs/17/functions-admin.html),
[SQLAlchemy async sessions](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html).
