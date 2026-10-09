# Workspaces and provider connections

These features are always on. Each clinic may connect its own Twilio, speech, AI-model and embeddings accounts; anything it has not connected is served by the platform account configured in the environment.

Run `python -m app.setup` before starting the API and the worker. It applies every migration, provisions the restricted API and worker logins, and runs the optional Super Admin bootstrap. See [fresh installation](fresh-install.md).

## Required secrets

All of these are platform infrastructure. Inject them from the deployment secret manager; they are never tenant-editable and never stored in PostgreSQL.

| Setting | Needed for | Notes |
| --- | --- | --- |
| `SECRET_ENCRYPTION_KEYS`, `SECRET_ENCRYPTION_ACTIVE_KEY` | Two-step verification, queued email, provider connections | JSON keyring `{"key-id": "<base64 of 32 random bytes>"}`. New data is encrypted with the active key. Keep older key IDs in the keyring until nothing references them. A lost key makes its ciphertext unrecoverable, so back the keyring up separately from the database. |
| `STREAM_TOKEN_KEY` | Integrations with telephony | Independent random value, at least 43 characters. Signs media-stream tokens so a provider secret is never used as an application key. |
| `SMTP_*` | Invitations and password reset | TLS is required outside local/development. Mail is delivered by the worker (`python -m app.worker`). |
| `TENANT_AUTH_PUBLIC_ORIGIN` | Invitation links | The frontend origin. HTTPS outside local/development. |

The API and the worker (`python -m app.worker`) both need the keyring.

## Creating a workspace

1. `POST /api/v1/platform/tenant-onboardings` with an `Idempotency-Key` creates the workspace, its first location and an invited (not yet active) administrator in one transaction. Retrying with the same key returns the same workspace; reusing the key with a different body returns 409. The new workspace starts with `service_state: "setup"`.
2. `GET` / `PATCH /api/v1/platform/tenants/{tenant_id}/onboarding` resume and edit the draft under a `revision` guard. Editing is refused once the administrator has accepted. Changing the administrator's email cancels any invitation already sent.
3. `POST /api/v1/platform/tenants/{tenant_id}/admin-invitations` queues a single-use, 24-hour invitation. A new idempotency key resends and invalidates the previous link. The response and the onboarding view report delivery state (`not_sent`, `queued`, `sent`, `delivery_failed`, `expired`, `accepted`), never the token.

How the invited person accepts and signs in is described in [sign-in and accounts](sign-in.md).

A workspace goes live once an administrator has accepted, its configuration is published and every required provider is available.

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
| `llm` | `gemini`, `openrouter` | `api_key` | `model`, limited to `TENANT_LLM_MODELS`; optional `thinking_level` |
| `embeddings` | `gemini`, `openrouter` | `api_key` | `gemini-embedding-2` or `openai/text-embedding-3-small`, both at 1536 dimensions to match the index |

There is one connection per purpose; speech has no separate fallback slot.

Behaviour to rely on:

- Responses contain `has_secret`, a four-character `display_suffix`, `revision`, `status` and timestamps. There is no endpoint that returns a stored secret.
- Each submission is an immutable, encrypted version. Its ciphertext is bound to the tenant, connection, provider, purpose and version number, so it cannot be decrypted under another row.
- Verification calls a fixed provider host with a timeout and no redirects. `verified` promotes (or marks the version promotable), `invalid` marks only that version, and `unavailable` changes nothing so the check can be repeated. A failed or unreachable check never displaces the version that is serving calls.
- Tenants cannot supply URLs, hosts or headers. Azure's region is a closed list and is fixed while a credential exists.
- Every mutating call carries the connection's current `revision`; a stale revision returns 409.
- Disconnecting erases the secrets in our database. It does not revoke the key at the provider; do that in the provider's console.
- A phone number is bound only after the provider confirms the connected account owns it. The response gives the two webhook URLs to set on the number in Twilio. Nothing is purchased or reconfigured at the provider.

## How calls use tenant credentials

- An incoming webhook selects a candidate tenant from the destination number, then must carry a valid signature and a matching `AccountSid` from that tenant's Twilio account: its own connection, or the platform account when it has none. Only then is the tenant established.
- The call row records the connection and credential version it was accepted on. Later status callbacks, the media stream and transfers must authenticate against that same connection.
- Speech, AI-model and embeddings providers are resolved from the call's or job's tenant at the time of use. Circuit breakers are kept per connection, so one tenant's provider failures do not open another tenant's circuit.
- Each provider is the tenant's own verified connection when it has one, otherwise the platform account from the environment. Another tenant's connection is never used. With neither, the call or job fails (`integration_unavailable`) before the caller hears anything.
- `service_state` is separate from `is_active`. A workspace in `setup` can be administered but receives no calls. `activate` recomputes readiness in the same transaction that flips the state.

## Not covered here

- Platform-managed (subaccount) connections are modelled (`ownership`) but only tenant-owned credentials can be submitted.
- Verification proves a credential is accepted. It does not place a test call; a real-provider canary remains a release step.
- Tenant-level spend limits and usage attribution are not implemented.
