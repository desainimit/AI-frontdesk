# Super Admin tenant support access

Support access uses a separate `fds1` bearer token. It does not create a tenant user, issue a tenant `fd1` credential, or change the Super Admin's platform identity. The token is bound to one active tenant, one platform user, and the MFA-verified platform login session that created it. The database stores only its SHA-256 digest. It expires after at most 30 minutes and no later than the parent platform session.

Apply all current migrations and reprovision the restricted runtime role before starting the updated API. The normal `migrate` Compose service runs both steps. A local virtual environment can run `python -m alembic upgrade head` followed by `python -m app.core.database.provision` with owner and runtime URLs reachable from that process. Follow the global identity cutover requirements in [implementation status](saas-implementation.md).

1. Authenticate as a Super Admin at `POST /api/v1/platform/auth/login` with a current TOTP code and retain the `fps1` token. Password-only sessions are limited to MFA enrollment and verification; they cannot enter a tenant.
2. Send that token to `POST /api/v1/platform/support-sessions` with a tenant ID and an access reason of 3–500 characters. Omitted capabilities grant all implemented tenant business permissions. An optional explicit capability list can narrow access. Duration defaults to 30 minutes and may be shortened to 1–30 minutes. No tenant membership or tenant approval is required.
3. Use the returned `fds1` token for allowed tenant endpoints. `GET /api/v1/support/me` reports the real platform actor, effective tenant, capabilities, and expiry. Do not use `/users/me` for support access; it identifies actual tenant users only.
4. End the session with `POST /api/v1/support/exit` using the `fds1` token, or revoke it from a platform session with `POST /api/v1/platform/support-sessions/{tenant_id}/{session_id}/revoke`. Logging out or disabling the parent platform account also invalidates child support sessions.

For example, an entry body is:

```json
{
  "tenant_id": "00000000-0000-0000-0000-000000000001",
  "reason": "Investigating a reported tenant directory issue",
  "capabilities": ["tenant:read", "directory:read"],
  "duration_minutes": 10
}
```

The backend permission catalog owns the available capability keys, including role, configuration, team and integration operations. The legacy `confirm_sensitive` field is accepted for compatibility but is no longer an additional authorization requirement. Super Admins use the same revision checks and validated business services as tenant administrators. Platform operations continue to use the separate `fps1` token and platform permissions. `/users/me` remains a tenant-user identity endpoint; support access uses `/support/me`. The frontend displays a persistent platform-access banner and returns to the platform console on exit. An explicit route allowlist denies new tenant endpoints until reviewed.

Every authorized or denied support route attempt creates an append-only `platform_audit_events` record with the platform actor, support session, tenant, request ID, method, and route template. The attempt record is committed before the domain operation; it does not claim that the operation succeeded. Entry, exit, and revocation have separate records. Domain services that write tenant audit events retain `actor_type=platform_support`, `platform_user_id`, and `support_session_id`; they never claim a tenant `actor_id`. Runtime operations set a transaction-local tenant scope and revalidate the support session before accessing tenant data. Forced PostgreSQL RLS remains active on all tenant tables, including `support_sessions`.
