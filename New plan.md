# Production AI Frontdesk — tenant-configurable SaaS

## 1. Product direction and configuration boundary

Build the complete front office suite: voice, transactional SMS, web chat, email, CRM, tickets, scheduling, consented outbound calls, workflows, analytics, subscriptions, and usage billing.

Every supported operational setting must be configurable by an authorized tenant administrator without code changes, deployments, database edits, or super-admin assistance.

Your selections establish these boundaries:

- Operational settings are dynamic.
- Record structures, implemented modules, permission keys, and supported workflow types remain application-defined.
- No arbitrary custom-field, custom-module, or general workflow-builder functionality in this release.
- Tenant branding covers business identity, logos, and chat widgets.
- The operator dashboard retains one shared professional zinc design with light/dark themes.
- One configurable role per user per tenant.
- Super admin has full business administration of every tenant.
- Users receive single-use password setup or membership acceptance links.

“Dynamic” means saved tenant configuration controls actual runtime behavior. An editable form backed by hardcoded agent behavior does not satisfy this requirement.

Retain enforced platform boundaries for tenant isolation, credential security, financial integrity, provider capabilities, consent, and purchased entitlements.

## 2. Tenant configuration system

### Tenant-admin controls

| Area | Tenant-configurable operations |
|---|---|
| Business | Name, contact details, logo, addresses, time zone, locations, business hours, holidays |
| Services | Offerings, descriptions, prices, durations, buffers, booking rules, assigned staff/resources |
| Staff and resources | Availability, assignments, capacity, transfer destinations, escalation contacts |
| Agent | Greeting, tone, supported business instructions, clarification behavior, escalation rules, permitted actions |
| Languages and speech | Enabled languages, default language, code-mixing preferences, compatible voices, supported speech profiles |
| Knowledge | Sources, FAQs, approved business facts, document versions, publication and archival |
| Voice | Numbers, agent/location routing, after-hours behavior, transfer rules, recording preference, call-duration preference |
| Scheduling | Connected calendars, booking windows, minimum notice, cancellation/rescheduling rules, confirmations |
| Web chat | Allowed domains, widget branding, greeting, agent assignment, escalation |
| SMS and email | Connections, approved templates, supported variables, reminder content, delivery preferences |
| CRM and tickets | Assignment rules, default owners, supported routing/escalation settings |
| Workflows | Enable/disable supported workflow types, trigger parameters, delays, recipients, schedules, retry preferences |
| Integrations | Managed/BYOK selection, verified connections, mappings, supported synchronization options, webhooks |
| Users and roles | Invitations, role definitions, permission matrices, role assignments, session revocation |
| Usage and billing | Subscription selection, tenant budget, alert thresholds, top-ups, optional auto-recharge |
| Privacy | Recording consent settings, supported retention periods, exports, deletion requests |
| Notifications | Operational recipients, enabled notification types, supported template content |

Workflow types initially include appointment reminders, missed-call follow-up, consented callbacks, lead assignment, ticket escalation, and post-call synchronization. Tenants configure these supported types; they do not introduce arbitrary execution logic.

Initial Owner/Admin roles receive operational configuration permissions. Other roles can receive selected configuration capabilities.

### Storage and configuration resolution

Use relational tables for locations, hours, services, staff, resources, routing, and integration bindings. Use typed, versioned documents for agent behavior and policy settings.

Store secret references separately from configuration. Never return credential values through configuration APIs.

Implement:

- Server-side drafts with revision numbers.
- Validation against permissions, entitlements, provider capabilities, and business constraints.
- Preview and safe testing.
- Atomic publication of immutable configuration revisions.
- Revision history, diffs, rollback, and audit attribution.
- Runtime references to the published revision.

Copy versioned platform defaults into the tenant’s initial configuration. They become editable starting values. Updating a platform template must not silently change existing tenant behavior.

Resolve permitted overrides in this order:

1. Tenant configuration.
2. Location override.
3. Agent override.
4. Channel override.

Define eligible fields for each scope. For example, location hours and agent greetings have different ownership. Do not apply unrestricted deep merging.

Show the effective value and its origin in configuration screens. Provide an explicit “use inherited value” action.

Conversations pin published behavioral configuration. Booking tools still check current availability and business constraints. Permission revocation, consent withdrawal, credential revocation, and hard usage limits apply immediately.

Queued workflows retain their published workflow configuration but recheck authorization, consent, current resource state, and channel availability before dispatch.

### Capabilities and APIs

Expose backend-owned catalogs for:

- Modules and actions.
- Configuration sections and constraints.
- Compatible providers, models, voices, and languages.
- Supported workflow types and parameters.
- Notification template variables.
- Effective entitlements and limits.

Frontend options come from these catalogs. Remove independently maintained language, provider, and permission lists.

An unavailable option remains visible where needed with its reason: unsupported language, missing connection, insufficient entitlement, or incomplete approval.

Add `/api/v2` interfaces for configuration drafts, validation, testing, publishing, history, rollback, and effective configuration. Use revision preconditions and typed responses.

Saving configuration must result in the corresponding runtime behavior. Publishing must invalidate relevant caches across all instances through durable events.

### Tenant and platform authority

Tenant admins can configure their workspace within granted permissions and purchased capabilities.

They cannot raise their purchased concurrency, alter platform prices, remove consent requirements, grant platform privileges, or disable isolation.

Super admin uses the same validated configuration services with broader authority. Changes remain versioned and audited; no separate database-edit path.

Disabling a channel stops new work and handles pending work explicitly. Preserve historical records and administrator access to configuration and recovery.

## 3. Dynamic RBAC, super admin, and onboarding

### Authorization

Replace hardcoded role-name permissions with tenant-owned roles and permission records:

```json
{
  "module": "agent",
  "permission": "publish",
  "allowed": true
}
```

Each membership references one role. A user may have different roles across tenants.

Rules:

- Missing permissions default to false.
- Unknown permission keys are rejected.
- Tenant roles cannot grant platform permissions.
- Role managers cannot grant beyond their own grantable authority.
- Permission prerequisites are validated.
- Assigned roles require reassignment before deletion.
- Prevent removal of the last active member capable of managing users and roles.

Seed editable Owner, Admin, Operator, Analyst, and Billing templates. Names do not determine access.

Backend services enforce current permissions and tenant ownership. Sensitive writes recheck authorization inside the transaction. Return effective capabilities and authorization revision to the frontend.

Permission changes affect the next authorized request and invalidate affected client data and subscriptions.

### Super admin

Use a protected platform Super Admin role with full implemented platform and tenant business permissions.

Super admin can create and administer tenants, configure agents and channels, manage users/roles, publish configuration, manage subscriptions, rotate credentials, and perform audited financial adjustments.

Tenant entry:

- Requires platform authentication and MFA.
- Requires no tenant membership or approval.
- Establishes a tenant-scoped administrative context.
- Records the access reason.
- Displays a persistent super-admin banner.
- Expires after 30 minutes and can be renewed.
- Returns to the platform console on exit.

Keep RLS active. Record the real platform actor on every operation. Passwords and provider secrets remain write-only; historical audit and financial records remain immutable.

### Onboarding

Use the same resumable flow for self-service and super-admin-assisted creation:

1. Business, locations, hours, and time zone.
2. Initial administrator identity.
3. Roles and optional team invitations.
4. Agent, languages, services, routing, and permissions.
5. Knowledge ingestion and approval.
6. Channels and integrations.
7. Billing and budget.
8. Tests and readiness checks.
9. Publish and activate.

Create a setup-state tenant and pending owner membership idempotently. Save drafts server-side.

Email workspace details, login URL, assigned role, and a single-use acceptance/setup link. New identities choose their password; existing identities accept membership without resetting it.

Links expire after 24 hours. Resending invalidates previous unused links. Track mail acceptance, failure, expiry, and user acceptance separately.

Activation requires an accepted administrator, published configuration, sufficient billing state, and verified readiness for enabled channels.

Tenant admins can revisit and change operational settings after onboarding. The wizard must not be the only place configuration exists.

## 4. Complete frontend redesign and interaction standards

### Appearance and shared components

Replace the current blue/slate styling across authentication, onboarding, tenant operations, and platform administration.

Use neutral zinc:

| Element | Light | Dark |
|---|---|---|
| Background | `#FAFAFA` | `#09090B` |
| Card/popover | `#FFFFFF` | `#18181B` |
| Text | `#18181B` | `#FAFAFA` |
| Muted surface | `#F4F4F5` | `#27272A` |
| Muted text | `#71717A` | `#A1A1AA` |
| Primary action | Charcoal with light text | Light with charcoal text |

Use semantic tokens, accessible status colors, stronger interactive boundaries, consistent focus treatment, Geist typography, an eight-pixel base radius, and restrained shadows.

Separate primitives, shared patterns, and feature components. Share shells, configuration editors, forms, tables, filters, loaders, errors, confirmations, and permission gates.

Provide light/dark/system themes without flashes. Business logos and widget colors are configurable; dashboard colors and layouts remain shared.

### Responsive tables and layouts

Use one adaptive layout:

- Below 768 px: drawer navigation and stacked controls.
- 768–1023 px: compact layouts.
- At least 1024 px: persistent sidebar and wider panels.

Keep tables as semantic tables on every viewport.

Requirements:

- One bounded scroll region for both axes.
- Sticky opaque header.
- Readable minimum column widths.
- Filters and pagination outside the scrolling region.
- Default maximum height `min(65dvh, 44rem)`.
- Optional explicit pinned columns.
- Cursor pagination with 25 rows by default.
- No fabricated totals or mobile card conversion.
- No document-level horizontal overflow.

### Data fetching and states

Use URL-backed list state, SSR initial data, and hydrated TanStack Query with one fetch owner.

Search uses 350 ms debounce; Enter/clear apply immediately. Respect composition input and preserve pending search across other filter changes.

Filter, sort, and page-size changes reset pagination. Apply validated date ranges in the workspace time zone.

All list operations call backend queries. Consume cancellation signals and reject stale responses. Tenant and authorization context belong in query keys.

Provide consistent initial loading, updating, empty, no-match, offline, forbidden, expired-session, and failed states.

Preserve input after errors. Prevent duplicate writes. Unknown outcomes require reconciliation rather than blind retries or false success.

Configuration editors additionally show:

- Draft versus published values.
- Inherited versus overridden values.
- Validation and readiness issues.
- Unsaved changes.
- Publication impact.
- Revision conflicts and recoverable edits.
- Change history and rollback.

## 5. Delivery, verification, and production defaults

### Architecture and delivery order

Retain FastAPI, SQLAlchemy/Alembic, PostgreSQL with RLS/pgvector, Pipecat, Next.js, TypeScript, TanStack Query, and Zod.

Use separate control, voice-runtime, and durable-worker processes. Use Temporal for durable workflows and Valkey for ephemeral coordination.

Implement in dependency order:

1. Freeze repositories, reconcile concurrent changes, fix typing, and establish CI.
2. Establish the new design system and shared interaction components.
3. Implement global identity, dynamic RBAC, and super-admin tenant entry.
4. Build the tenant configuration engine and resumable onboarding.
5. Add managed provisioning, BYOK, subscriptions, usage reservations, and limits.
6. Implement configurable agents, approved knowledge, language profiles, and safe tools.
7. Deliver real voice, scheduling, and human handoff.
8. Deliver channels, CRM, tickets, and external synchronization.
9. Deliver configurable supported workflows and event-based analytics.
10. Complete operational, security, accessibility, accuracy, and capacity gates.

Use Exotel initially for India voice; Google/Microsoft calendars and mail; HubSpot; Razorpay; Azure/Sarvam speech profiles. Verify provider capabilities and quotas before enabling tenant choices.

### Acceptance criteria

The key dynamic-configuration test is:

**Two tenants configure different business hours, services, greetings, languages, routing, reminders, and permissions. Their real conversations and workflows follow those settings independently, without deployment or platform intervention.**

Also verify:

- Changes survive restarts and operate consistently across instances.
- Draft changes do not affect published behavior.
- Invalid or incompatible configurations cannot publish.
- Rollback restores a valid previous configuration.
- Super-admin and tenant-admin edits use the same revision checks.
- Concurrent edits do not silently overwrite each other.
- No tenant setting leaks into another tenant’s runtime or UI.
- Changing platform defaults does not mutate existing tenants.
- Revocation and consent changes override queued work immediately.

Test RBAC, RLS, invitations, provisioning, payments, duplicate callbacks, unknown outcomes, booking conflicts, and provider failures using real integration infrastructure.

Frontend tests cover both themes, representative viewport widths, zoom, keyboard access, long multilingual content, both table scroll axes, debounce races, stale responses, tenant switching, and all loading/error states. Target WCAG 2.2 AA.

Launch English, Hindi, Hinglish, Gujarati, Spanish, French, German, and Portuguese only after profile-specific evaluation. Require at least 95% task accuracy, 98% action-critical entity accuracy, and 99% supported factual claims in the evaluated set, with zero unauthorized writes or false completion confirmations.

Demonstrate two hours at 100 simultaneous calls, controlled overload rejection, real-carrier validation, deployment draining, payment reconciliation, backup restoration, and deletion.

### Defaults are starting values

Initial defaults remain:

- Time zone: `Asia/Kolkata`.
- Call duration preference: 20 minutes.
- Outbound quiet hours: 20:00–08:00.
- Maximum three automatic attempts per destination per 24 hours.
- Transcript retention: 90 days.
- Consented audio retention: 30 days.
- Recording disabled.
- Auto-recharge disabled.

Tenant admins may change supported preferences within published platform/provider constraints. Expose those constraints through backend capability metadata rather than duplicating them in frontend constants.

Deploy in AWS Mumbai with Multi-AZ PostgreSQL, private services, encrypted storage, monitored queues, and explicit voice-worker draining.

Release through internal verification, consenting full-suite pilots, and controlled public admission. Every configurable setting must have tested persistence, authorization, validation, runtime application, and audit behavior before production approval.
