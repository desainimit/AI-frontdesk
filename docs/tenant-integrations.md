# Tenant onboarding, login and provider connections

This covers three features that are off by default and enabled independently:

| Flag | What it turns on |
| --- | --- |
| `TENANT_ONBOARDING_ENABLED` | Platform creates a draft workspace with a pending administrator. No API token is issued. |
| `TENANT_HUMAN_AUTH_ENABLED` | Email/password login for tenant users, emailed invitations, OTP password reset, team management. |
| `TENANT_INTEGRATIONS_ENABLED` | Each tenant connects its own Twilio, speech, AI-model and embeddings accounts. Calls and jobs use only that tenant's credentials. |

Run `python -m app.database.initialize` before starting the API and the worker. It applies every migration, provisions the restricted API and worker logins, and runs the optional Super Admin bootstrap. See [fresh installation](fresh-install.md).

## Required secrets

All of these are platform infrastructure. Inject them from the deployment secret manager; they are never tenant-editable and never stored in PostgreSQL.

| Setting | Needed for | Notes |
| --- | --- | --- |
| `SECRET_ENCRYPTION_KEYS`, `SECRET_ENCRYPTION_ACTIVE_KEY` | Human auth, integrations | JSON keyring `{"key-id": "<base64 of 32 random bytes>"}`. New data is encrypted with the active key. Keep older key IDs in the keyring until nothing references them. A lost key makes its ciphertext unrecoverable, so back the keyring up separately from the database. |
| `OTP_HMAC_KEY` | Human auth | Independent random value, at least 43 characters. |
| `STREAM_TOKEN_KEY` | Integrations with telephony | Independent random value, at least 43 characters. Signs media-stream tokens so a provider secret is never used as an application key. |
| `SMTP_*` | Invitations and password reset | TLS is required outside local/development. Mail is delivered by the worker (`python -m app.worker`). |
| `TENANT_AUTH_PUBLIC_ORIGIN` | Invitation links | The frontend origin. HTTPS outside local/development. |

The API and every worker that touches tenant credentials (knowledge worker, workflow worker, mail worker) need the keyring.

## Onboarding and administrator access

1. `POST /api/v1/platform/tenant-onboardings` with an `Idempotency-Key` creates the tenant, its first location and an inactive administrator in one transaction. Retrying with the same key returns the same workspace; reusing the key with a different body returns 409. The new tenant starts with `service_state: "setup"`.
2. `GET` / `PATCH /api/v1/platform/tenants/{tenant_id}/onboarding` resume and edit the draft under a `revision` guard. Editing is refused once the administrator has accepted. Changing the administrator's email cancels any invitation already sent.
3. `POST /api/v1/platform/tenants/{tenant_id}/admin-invitations` queues a single-use, 24-hour invitation. A new idempotency key resends and invalidates the previous link. The response and the onboarding view report delivery state (`not_sent`, `queued`, `sent`, `delivery_failed`, `expired`, `accepted`), never the token.
4. The invitee opens `{TENANT_AUTH_PUBLIC_ORIGIN}/tenant-auth/accept#token=...`. The frontend sends the bearer token in a POST body to `/api/v1/tenant-invitations/details` to learn whether password setup is required. New identities submit a password (15 to 128 characters) to `/api/v1/tenant-invitations/accept`; existing identities omit the password and retain their global credential. Acceptance activates only that membership and consumes the invitation atomically. A locked legacy identity must recover through email-verified password reset in an accepted workspace first.
5. `POST /api/v1/tenant-auth/login` with `tenant_id`, `email` and `password` returns an `fhu1` bearer token valid for 8 hours. It is accepted by the same tenant routes as `fd1` service tokens and is rechecked for revocation inside every database operation.

Password recovery: `POST /api/v1/tenant-auth/forgot-password` always answers 202, and `POST /api/v1/tenant-auth/reset-password` takes the emailed 8-digit code (10 minutes, 5 attempts). A successful reset changes the global credential revision, invalidating sessions and older reset codes across all accepted memberships. A tenant administrator can revoke only sessions in their own tenant. Ambiguous legacy memberships differing only by email case fail closed and require administrative reconciliation.

Team management, for a member with the relevant persisted grants: `POST /api/v1/users/invitations`, `POST /api/v1/users/{user_id}/invitations` (resend), `PATCH /api/v1/users/{user_id}` (role and active state) and `POST /api/v1/users/{user_id}/revoke-sessions`. Dynamic role management is exposed through `/api/v2/roles`. The last accepted, active member capable of managing users and roles cannot lose those capabilities. Pending invitations count only for initial setup configuration and cannot authorize live activation.

Login, acceptance and recovery are throttled per account and per source address and need Redis; without it they answer 503 rather than run unthrottled.

The legacy `POST /api/v1/platform/tenants` contract still returns a one-time `fd1` admin token and creates a tenant that is live immediately on the platform's provider accounts.

New SaaS onboarding requires accepted administration, published configuration and
funding readiness. Its activation remains blocked until funding and provider
dispatch are implemented; verified credentials alone do not activate it.

## Provider connections

Authorized members use `/api/v1/...`; an MFA-verified platform administrator acts on one named tenant through `/api/v1/platform/tenants/{tenant_id}/...` or an audited tenant entry context. These paths call the same validated services. Tenant callers and members lacking the required integration grants are refused.

| Route | Purpose |
| --- | --- |
| `GET /integration-providers` | Supported providers, slots and the allowlisted configuration schema. |
| `GET` / `POST /integrations`, `GET` / `PATCH /integrations/{id}` | Connection metadata and nonsecret configuration. One connection per purpose and slot. |
| `POST /integrations/{id}/credential-versions` | Write-only secret submission (`Idempotency-Key` required). |
| `POST /integrations/{id}/verify` | Check a version against the provider; `activate` (default true) promotes it on success. |
| `POST /integrations/{id}/activate-version` | Promote a version verified within the last hour. |
| `POST /integrations/{id}/disconnect` | Erase every stored secret for the connection and stop routing its numbers. |
| `GET` / `POST /integrations/{id}/phone-numbers` | List numbers owned by the connected Twilio account; bind one. |
| `GET /tenants/current/readiness`, `POST /tenants/current/activate`, `POST /tenants/current/deactivate` | Readiness checks and the live switch (platform: `/readiness`, `/activate`, `/deactivate` under the tenant path). |

Supported connections:

| Purpose | Provider | Secrets | Configuration |
| --- | --- | --- | --- |
| `telephony` | `twilio` | `auth_token`; optional `api_key_sid` + `api_key_secret` for REST | `account_identifier` (Account SID) |
| `stt` | `deepgram`, `azure_speech` | `api_key` / `speech_key` | model and language / region and language |
| `tts` | `elevenlabs`, `azure_speech` | `api_key` / `speech_key` | model and voice / region and voices |
| `llm` | `gemini` | `api_key` | `model`, limited to `TENANT_LLM_MODELS`; optional `thinking_level` |
| `embeddings` | `gemini` | `api_key` | `gemini-embedding-2` only, at 1536 dimensions to match the index |

`stt` and `tts` accept a `primary` and an optional `fallback` slot.

Behaviour to rely on:

- Responses contain `has_secret`, a four-character `display_suffix`, `revision`, `status` and timestamps. There is no endpoint that returns a stored secret.
- Each submission is an immutable, encrypted version. Its ciphertext is bound to the tenant, connection, provider, purpose and version number, so it cannot be decrypted under another row.
- Verification calls a fixed provider host with a timeout and no redirects. `verified` promotes (or marks the version promotable), `invalid` marks only that version, and `unavailable` changes nothing so the check can be repeated. A failed or unreachable check never displaces the version that is serving calls.
- Tenants cannot supply URLs, hosts or headers. Azure's region is a closed list and is fixed while a credential exists.
- Every mutating call carries the connection's current `revision`; a stale revision returns 409.
- Disconnecting erases the secrets in our database. It does not revoke the key at the provider; do that in the provider's console.
- A phone number is bound only after the provider confirms the connected account owns it. The response gives the two webhook URLs to set on the number in Twilio. Nothing is purchased or reconfigured at the provider.

## How calls use tenant credentials

- An incoming webhook selects a candidate tenant from the destination number, then must carry a valid signature from that tenant's own Twilio connection and a matching `AccountSid`. Only then is the tenant established.
- The call row records the connection and credential version it was accepted on. Later status callbacks, the media stream and transfers must authenticate against that same connection.
- Speech, AI-model and embeddings providers are resolved from the call's or job's tenant at the time of use. Circuit breakers are kept per connection, so one tenant's provider failures do not open another tenant's circuit.
- A tenant with a missing or disconnected connection fails closed (`integration_unavailable`). The provider keys in the environment are used only for tenant IDs listed in `LEGACY_PROVIDER_TENANT_IDS`.
- `service_state` is separate from `is_active`. A workspace in `setup` can be administered but receives no calls. `activate` recomputes readiness in the same transaction that flips the state.

## Migrating existing tenants

Migration `0019` marks every existing tenant `live` so nothing changes while `TENANT_INTEGRATIONS_ENABLED=false`.

Before setting it to `true`:

1. Decide, per existing tenant, whether it keeps the platform accounts for now. List those tenant IDs in `LEGACY_PROVIDER_TENANT_IDS`. Any tenant not listed and without its own verified connections stops taking calls.
2. Set `SECRET_ENCRYPTION_KEYS`, `SECRET_ENCRYPTION_ACTIVE_KEY` and `STREAM_TOKEN_KEY`, and give the workers the keyring.
3. Move tenants off the allowlist one at a time: connect and verify their accounts, bind their number through the platform route (a platform-assigned number can only be rebound by the platform), point the number's webhooks at the URLs returned, then remove the tenant ID from the list.

Rolling back to `TENANT_INTEGRATIONS_ENABLED=false` returns every tenant to the platform accounts and leaves stored connections, versions and call bindings intact. Numbers that were bound to a tenant connection are then refused until integrations are re-enabled, because their calls must be served by that connection.

## Not covered here

- Tenant-user multi-factor authentication is not implemented. Platform business administration requires TOTP MFA; recovery codes and operator factor recovery remain unfinished.
- Platform-managed (subaccount) connections are modelled (`ownership`) but only tenant-owned credentials can be submitted.
- Verification proves a credential is accepted. It does not place a test call; a real-provider canary remains a release step.
- Tenant-level spend limits and usage attribution are not implemented.
