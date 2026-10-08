# Tenant-configurable SaaS implementation

This document tracks implementation evidence for the approved production plan. A
feature is complete only when its API, persistence, authorization, UI, runtime,
and relevant checks are connected. Provider credentials and successful unit tests
alone do not constitute production approval.

**Status: foundations implemented; the complete approved SaaS plan is not delivered.**
Do not deploy this change as a completed front-office suite. New onboarding cannot
activate paid channels while payment capture, admission integration and provider
dispatch remain unfinished. Existing operator-bootstrap workspaces retain their legacy contract.

## Implemented and verified

| Area | Implementation | Limits |
|---|---|---|
| Authorization | Persisted tenant roles and explicit module/action grants; editable templates; grant envelope, prerequisites, tenant ownership, assigned-role deletion protection, last custodian protection; transactional authorization rechecks; section and scope authority; module publication includes owned overrides and rejects unrelated changes | Section authority does not establish provider dispatch or financial administration |
| Global identity | One normalized email/password identity with separate accepted tenant memberships; existing users accept without setting a password; credential revision invalidates sessions and outstanding reset codes across memberships; forced identity RLS and transaction-local email scope; email/password workspace chooser; email-only recovery routes a single-use mailbox OTP through an encrypted receipt bound to its challenge and email | No email-change or identity deletion flow yet; production mail delivery remains an external verification gate |
| Platform access | Encrypted TOTP enrollment, replay rejection, persistent lockout, MFA-required operations, tenant entry with reason and 30-minute expiry; hash-only single-use recovery codes; revision-checked rotation, recovery sign-in and authenticated lost-factor replacement; older sessions and tenant entry revoked during recovery | Total loss of both authenticator and recovery codes requires a separate operator recovery process; delivery of security notifications is not verified |
| Configuration | Typed versioned drafts, revision preconditions, validation, immutable publication, durable event, preview, history, staged rollback; explicit tenant/location/agent/channel ownership; bounded atomic scope commands, module-owned reads and dashboard editors, inherited values and effective published origins; relational hours and directory synchronized with drafts/rollback; restricted durable dispatcher and authoritative revision reconciliation | Provider compatibility remains unfinished; publication impact needs more resource-level detail |
| Business directory | Relational services, staff, resources, assignments, staff availability/holidays; staged revision checks, immutable idempotency receipts, owned references, rollback and retained history; published service/staff read tools exclude private contacts and retain call pins | Calendar bindings stay unavailable pending adapters; general booking and capacity reservations remain unfinished |
| Runtime binding | Published configuration and selected location/agent/channel pinned on call admission; explicit scoped resolution drives greeting, language, instructions, tone, tools/budgets, transfer and duration; disabled scope resources reject new calls and pending streams while active calls retain pinned behavior | Live activation of new voice configurations is blocked; scoped runtime checks use synthetic persisted calls, not real carrier proof; existing voice adapters remain Twilio-oriented and clinical tools remain |
| Onboarding | Idempotent setup-state creation, editable defaults and single-use invitations; shared resumable nine-stage tenant/platform guide; backend-owned readiness evidence and revision-checked reviews invalidate after relevant changes; server-saved position, module permissions and uncertain-write reconciliation | Self-service signup remains unfinished; the initial platform provisioning form remains short; guide reviews do not provision or activate channels |
| Billing | Immutable plan versions and financial ledger, separate restricted financial writer, enforced balance/ledger chain, prepaid subscription selection, pinned purchased prices/limits, idempotent audited platform adjustments; worker-only usage reservations, budgets, meter allowances, concurrency, fencing and verified-outcome settlement; tenant/platform dashboards and authenticated ledger cursors | Payment capture/refunds, invoices/tax, renewals, reservation sweeping, carrier/channel admission integration and auto-recharge remain unfinished; funding does not activate channels |
| Frontend | Shared zinc tokens, Geist, light/dark/system theme; bounded semantic tables with both scroll axes; 350 ms/IME search; backend capabilities; configuration/role editors; dynamic team; business directory with SSR-hydrated TanStack Query, server filters, revision-bound authenticated cursors and unknown-write reconciliation; workspace chooser/switching | Most other list pages still need conversion from server navigation/offset pagination to hydrated cursor queries |
| Infrastructure | Migrations 0020–0036, forced tenant/identity/financial RLS and restricted grants, repeatable empty-database initialization including the configured financial login, backend/frontend CI, digest-pinned BSD-licensed Valkey image, separate configuration-worker Compose service | AWS infrastructure and production voice/control separation are not completed by this change |
| Fresh workspace defaults | Versioned editable defaults, persisted roles/permission matrices, pending owner, first location, configuration draft, relational hours policy and setup progress; frontend obtains onboarding defaults from the backend | Actual hours, credentials, prices and funded balances require explicit configuration; no shared passwords or fake provider readiness |

No platform default is applied as a live update to existing tenant documents.
Publication is idempotent for the same saved draft revision. Draft changes do not
change the published pointer. Runtime admission reads PostgreSQL directly rather
than relying on a process-local configuration cache.

Catalog metadata disables unfinished voice, chat, scheduling, transactional SMS,
business email, recording, auto-recharge, and supported workflow activation.
This is an implementation boundary, not proof that those modules are complete.
Other saved preferences that lack a working dispatcher also require runtime
implementation and acceptance tests before they are treated as operational.

## Verification evidence

- Backend: Ruff lint/format and strict Pyright; 314 unit tests. The full
  integration run against task-created PostgreSQL and real Valkey passed 164
  tests. Verification includes conflicting credentials, ambiguous
  membership denial, reset-mail erasure and revision-bound publication review.
- PostgreSQL migration upgrade, restricted runtime provisioning, forced RLS,
  metadata comparison, immutable publication permissions, cross-tenant role
  binding, concurrent draft edits, independent tenants, rollback and revocation.
- Frontend: TypeScript, ESLint, 61 Vitest tests and optimized Next.js build;
  OpenAPI and generated permission/language catalogs match 133 backend paths.
- Empty-database rehearsal: all migrations from base, initialization twice,
  restricted role verification and fresh-workspace defaults before first read.
  Fifteen relevant integration checks passed, including the dependency health
  check rerun after starting the local Valkey service. See [fresh install](fresh-install.md).
- Billing browser verification: backend onboarding defaults, immutable pricing,
  audited adjustments, subscription selection, lost-response reconciliation,
  failed reconciliation recovery, backend ledger search, hydrated usage reads,
  both table scroll axes and six responsive light/dark captures.
- Real browser/BFF/backend verification in an isolated database: password login,
  MFA enrollment, tenant entry, saved draft preview/publication, effective
  comparison, history, persistence after reload, role creation and deletion. Configuration and role
  screens, scope/inheritance controls and publication review captured at 375, 768 and 1440 pixels in both actual themes (24 captures); no document
  overflow and permission tables scroll horizontally and vertically.
- Shared gallery/login checks cover both themes and representative widths,
  URL filter/search behavior, long names and multilingual content.
- Real invitation/BFF/backend browser checks cover new account password setup,
  existing-identity acceptance without password fields, and shared-password login
  to two isolated tenants. Twelve invitation captures at 375, 768 and 1440 pixels
  cover both actual themes without document overflow. Only synthetic invitations
  were used; no external email was sent by this verification.
- Scoped configuration: 24 relevant PostgreSQL checks passed after migration 0026;
  26 configuration/tenancy/onboarding checks passed after migration 0027, followed
  by a dedicated relational-hours persistence/rollback/RLS check. Scope creation
  replays preserve identity, revisions reject stale edits, and cross-tenant scope
  references and unsupported channel activation cannot publish. The browser flow
  verified scoped agent publication, source display and explicit inheritance.
- Business directory: five PostgreSQL checks cover immutable retry receipts, tenant
  isolation, HTTP query validation, keyset pagination, exact query/actor/revision
  cursor bindings, escaped search, pinned public facts, price changes, swapped-name
  rollback and preserved entity history. Browser checks verify 25-row pagination,
  one initial SSR fetch, pending search across filters and reconciliation after a
  committed write loses its response. The authenticated suite now produces 30
  light/dark captures, including directory scrolling on both axes.
- Workspace admission: 26 relevant identity/authentication/tenancy checks passed;
  three dedicated chooser checks include HTTP refresh throttling with real Valkey.
  Browser checks cover optional workspace-ID sign-in, two accepted memberships,
  switching, full sign-out and expired-session redirects without loops; six
  responsive chooser captures. The admission cookie is authenticated/encrypted
  and cannot be projected as a tenant session.
- Email-only recovery: four PostgreSQL/HTTP checks cover accepted membership,
  global revocation, unknown/pending identities, receipt email/challenge binding,
  tampering, expiry, resend invalidation and five committed attempts. Browser
  checks include HttpOnly receipt storage, confirmed recovery, a committed change
  with a lost response, safe sign-in reconciliation and six theme/viewport captures.
- Module settings: three PostgreSQL checks cover isolated reads/writes,
  invalid sections, revision conflicts, revocation, initial publication authority,
  filtered review and rejection of unrelated publication. Browser verification
  uses an accepted agent-only identity, verifies denied billing/privacy/full reads,
  successful agent publication, failed outcome-check protection, retained concurrent
  edits and immediate permission revocation; twelve responsive theme captures,
  including resource creation, bounded scope saves, failed refresh protection
  after confirmed writes, publication and explicit inheritance. Three additional
  PostgreSQL checks verify atomic mixed-permission rejection, cross-tenant IDs,
  invalid commands, bounded batches, own-scope reads and publication boundaries.
- Platform recovery: two PostgreSQL checks verify concurrent single-use consumption,
  password proof, replay/lockout, fresh TOTP rotation, generation conflicts, old
  session/tenant-entry revocation and protected replacement/re-enrollment. Browser
  checks cover enrollment, recovery admission, committed replacement with a lost
  response, failed outcome checks and a lost rotation response. Six responsive
  theme captures contain no recovery codes, passwords or setup keys.
- Publication delivery: three PostgreSQL/real-Valkey checks cover rollback on
  transport failure, concurrent SKIP LOCKED claims, monotonic notifications,
  immutable event bindings, restricted worker access and authoritative recovery
  after transport state loss. The real worker CLI passed startup and one-dispatch
  checks, and Compose configuration validates. The browser reconciles the shared
  revision pointer every ten seconds while visible, on focus and on reconnect;
  newer draft/publication notices preserve local edits. Runtime admission has no
  process-local behavioral configuration cache.

## Identity cutover

Migration 0024 preserves a legacy credential only when all accepted memberships
for the normalized email contain the same stored hash. Distinct salted hashes
cannot establish that plaintext passwords match, so conflicting hashes produce a
locked identity requiring the existing email-verified password reset flow. A
membership invitation cannot recover that identity or replace its password.
Migration 0025 erases undelivered superseded reset mail. Existing invitations
remain usable within their original validity window.

The cutover is forward-only. Rehearse it on a restored backup, confirm operational
reset mail delivery, and verify a restore before applying it to production. The
tested migration rehearsal used rollback-isolated legacy tables; production
backup restoration and SMTP delivery are still release gates.

These checks do not establish WCAG 2.2 AA compliance, the stated language accuracy
targets, real-carrier reliability, 100-call capacity, or production recoverability.
The existing audio pipeline still uses Python's deprecated `audioop` module and
is supported here on Python 3.12; upgrading to Python 3.13 requires its replacement.

## Remaining implementation, in dependency order

1. Recovery notifications and an operator process for total factor loss;
   migration/cutover of legacy bootstrap and
   direct configuration contracts; self-service provisioning and channel test evidence.
2. Remaining location/routing editors and general booking resource state;
   historical revision comparisons,
   resource-level publication impact, provider/language/voice compatibility
   and entitlement metadata.
3. Razorpay verification and reconciliation, subscription invoices/tax and renewals,
   managed provisioning/BYOK lifecycle, reservation sweeper and runtime admission
   integration, budget alerts, top-ups and idempotent auto-recharge.
4. General business contacts and tools, approved knowledge publication, proper
   Gujarati and Hinglish profiles, language evaluation, provider capability checks,
   truthful action completion and immediate consent/credential/authorization
   revocation throughout the runtime.
5. Real Exotel media/carrier integration, Google/Microsoft calendar locking and
   booking reconciliation, human transfer confirmation, transactional SMS, web
   chat, business email, CRM, tickets and HubSpot synchronization.
6. Temporal-backed implementations of the six supported workflow types, pinned
   workflow revision and dispatch-time checks, retries/dead-letter handling and
   event-derived analytics. Existing workflow intents are not the full requirement.
7. Backend cursor list contracts and hydrated TanStack Query migration; shared
   full-state UX, workspace-time-zone date queries, accessible configuration
   resource pickers, responsive/zoom/keyboard and race verification for every page.
8. AWS Mumbai private deployment, Multi-AZ PostgreSQL, managed secrets/storage,
   control/voice/worker separation, monitored durable queues, voice draining,
   backups/restoration, exports/deletion, consented pilots and controlled admission.

Every item requires application work. External credentials alone cannot complete
the remaining suite. Do not replace this list with a claim that only deployment or
provider approval remains.

## Required delivery

- Dynamic tenant roles, effective capabilities, revocation, and attributed platform access.
- Versioned configuration, validation, publication, rollback, and runtime binding.
- Resumable onboarding with single-use account setup.
- Zinc light/dark redesign and shared responsive tables, forms, and request states.
- General business records, paid provisioning, channels, scheduling, workflows, and billing.
- Provider integration, security, language evaluation, capacity, recovery, and accessibility gates.

## External release gates

Exotel account approval and quotas; payment merchant approval; Google/Microsoft
OAuth applications; mail delivery; speech and model credentials; AWS production
accounts; language-reviewed evaluation data; and real-carrier load tests must be
verified independently. Unconfigured services must fail closed and expose
actionable readiness status.

## Source preservation

Existing staged changes and development data are preserved. New migrations are
forward-only additions to the current migration history. No existing credentials
are printed, copied into documentation, or committed.
