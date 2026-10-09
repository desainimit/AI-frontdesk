# Sign-in and accounts

One account per email address signs in everywhere: clinic staff and platform
administrators use the same page, the same routes and the same kind of session.

## What a session is

`POST /api/v1/auth/login` takes an email and a password and returns one opaque bearer
token (`fs1.<id>.<secret>`). Only its SHA-256 digest is stored. The session row holds:

- the account,
- whether a two-step code is still owed,
- the workspace the session is working in.

The workspace is chosen on the server. No route reads a workspace identifier from the
browser to decide where a request applies; an `X-Tenant-ID` header is ignored.

Every response to a sign-in step says what to show next:

| `next` | Meaning |
| --- | --- |
| `mfa` | The account has two-step verification on. Send the code to `POST /auth/login/mfa`. Until then the token opens nothing else. |
| `workspace` | The person belongs to exactly one workspace and is now in it. |
| `choose_workspace` | Several workspaces (or none). List them with `GET /auth/workspaces`, pick one with `POST /auth/workspace`. |
| `platform` | A platform administrator; show the platform console. |

A workspace route called with no workspace selected answers `409` with code
`workspace_required`.

Sessions last 8 hours for workspace members and `PLATFORM_SESSION_MINUTES` (default 30)
for platform administrators. Each database transaction rechecks the session, so signing
out, a password change, a removed membership or a closed workspace takes effect on the
next statement, not at the next sign-in.

## Routes

All under `/api/v1`. Responses are `Cache-Control: no-store`.

| Route | Purpose |
| --- | --- |
| `POST /auth/login`, `POST /auth/login/mfa`, `POST /auth/logout` | Sign in, finish with a code or a recovery code, sign out. |
| `GET /auth/me` | The account, whether two-step is on, and the current workspace with the caller's role and permissions. |
| `GET /auth/workspaces`, `POST /auth/workspace`, `POST /auth/workspace/leave` | List, choose and leave a workspace. |
| `POST /auth/password/change` | Needs the current password. Other sessions end; this one continues. |
| `POST /auth/password/forgot`, `POST /auth/password/reset` | Email a single-use link (30 minutes), then set a new password with its token. Every session ends. |
| `POST /auth/mfa/setup`, `/enable`, `/disable`, `/recovery-codes` | Two-step verification (below). |
| `POST /auth/invitations/details`, `/accept` | What an invitation link shows, and accepting it. |
| `POST /users/invitations`, `POST /users/{id}/invitations`, `PATCH /users/{id}`, `POST /users/{id}/revoke-sessions` | Team management inside the current workspace. |
| `POST /platform/workspaces/{id}/open` | A platform administrator enters a workspace (below). |

`forgot` always answers `202`, whether or not the address has an account.

Passwords are 12 to 128 characters and are stored as salted scrypt hashes.

## Two-step verification

Optional for every account, switched on by its owner.

1. `POST /auth/mfa/setup` returns a secret, the `otpauth://` URI and a QR image
   (an SVG data URI) for an authenticator app. Valid for 10 minutes.
2. `POST /auth/mfa/enable` with one code turns it on and returns eight recovery codes,
   shown once. Other sessions of the account are signed out.
3. From then on each sign-in needs a code, or one unused recovery code. Using a
   recovery code signs every other session out.
4. `POST /auth/mfa/recovery-codes` (needs a code) replaces all recovery codes.
   `POST /auth/mfa/disable` needs the password and a code or recovery code.

A code works once. Five wrong codes lock the factor for 15 minutes. The secret is
encrypted with `SECRET_ENCRYPTION_KEYS`; recovery codes are stored as digests.

## Invitations

An invited person is a membership that is not active yet, linked to the account for
their email address (created without a password if the address is new).

- The emailed link is `{TENANT_AUTH_PUBLIC_ORIGIN}/invite#token=...`. It works once and
  expires after 24 hours. Sending again replaces it.
- A new account chooses its password when accepting and is signed in immediately.
- An existing account accepts without a password and keeps its own; an invitation can
  never replace a password.

The first administrator of a new workspace is invited by a platform administrator with
`POST /platform/tenants/{id}/admin-invitations`; team members are invited from the
workspace.

## Platform administrators

The setup job creates the first one from `SUPER_ADMIN_EMAIL` and `SUPER_ADMIN_PASSWORD`.
The API's database login cannot set or change the administrator flag on an account.

To work inside a workspace, an administrator calls
`POST /platform/workspaces/{id}/open` with a reason (3 to 500 characters). For up to
30 minutes their session works in that workspace with an administrator's permissions:

- they act as themselves, never as a member: audit rows carry their account and the
  visit, with no member id;
- every request is checked against a route list and written to the platform activity
  log before it runs; a route not on the list is refused;
- one workspace at a time; `POST /auth/workspace/leave`, signing out or the time limit
  ends the visit.

## Throttling

Sign-in, the code step, password change, forgot, reset and invitation routes are
limited per subject (5 per 5 minutes) and per address (20 per 5 minutes) in Redis.
Without Redis they answer `503` instead of running unthrottled.

## Activity log

`platform_audit_events` records sign-ins, sign-outs, password and two-step changes and
workspace visits against the account. It is append-only (database triggers refuse
updates and deletes) and the API login can add rows but not read them.
