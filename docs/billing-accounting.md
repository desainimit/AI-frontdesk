# Billing accounting boundary

Migration `0034_billing_accounts` adds immutable plan versions, tenant accounts,
ledger entries and command receipts. Amounts are integer INR paise; no floating
point arithmetic, editable historical balances or tenant-defined platform prices
are accepted.

The ordinary API database login has SELECT access only to financial records.
Inject a separate `FINANCIAL_DATABASE_URL` for the same database and run
`python -m app.billing.provision` with the schema-owner migration connection.
The financial login is a non-owner, cannot bypass RLS, and has no access to
unrelated business records, credentials, password hashes or session-token hashes.
Immutable tables grant no UPDATE, DELETE or TRUNCATE. Startup and each financial
transaction check this boundary. Never reuse the API, worker or schema-owner login.

Control-plane transactions hold current authorization and a tenant mutex while
the separate financial transaction records the command. The mutex uses FOR NO KEY
UPDATE so foreign-key checks in the financial connection can acquire key-share
locks without a cross-connection deadlock. The financial ledger itself records
the real actor, request, reason, amount and resulting balances; pricing records
the platform actor and request. Control-plane audit events are additionally queued
in the authority transaction. A dropped commit response remains uncertain and is
reconciled through the immutable financial command receipt.

Database triggers enforce the ledger's revision and amount chain. Deferred checks
require every balance projection to match its latest immutable ledger entry.
Platform credits/debits and price publication additionally require a current,
unrevoked, MFA-verified platform session through RLS. Existing prices and purchased
subscriptions are immutable; publication does not change an existing subscription.

Tenant billing managers select a published version with an account revision and
idempotent command reference. Selection debits the published prepaid monthly fee
once and pins that version for one calendar month in UTC, with month-end clamping.
Active subscriptions cannot be replaced or charged again. Period renewal is
explicit; automatic renewal and proration are not implemented. Other role names
do not establish billing authority. Overdrafts and removal of reserved funds are
rejected. Retrying the same reference with a different body or actor is rejected.

The tenant dashboard exposes account status, plans and a bounded ledger with
authenticated seek cursors. Search and operation filters execute on the backend.
Cursor context includes tenant, actor, session, role revision, query and page size.
The platform console publishes reviewed price versions and records justified
adjustments. Neither surface displays a payment success without a verified record.

Migrations 0035–0036 add worker-only usage reservations with pinned rates,
subscription-period meter limits, published tenant budgets and voice concurrency.
The budget uses the published business time zone and calendar month, excludes
subscription fees and includes every outstanding hold. The API exposes read-only
usage totals; tenant requests cannot report provider outcomes or fabricate settlement.

Reservation and dispatch commands serialize on the tenant. Undispatched holds
expire after 60 seconds. Dispatched work receives a fence and 30-second renewable
lease; an expired dispatch moves to reconciliation while retaining funds and its
call slot. A trusted adapter must verify the terminal provider outcome before
settlement. Duplicate receipts never renew an expired dispatch authorization.
Channel, account, budget and entitlement checks run on admission, start,
heartbeat and explicit dispatch authorization. Zero-price meters still consume
unit allowances and call slots. Settlement uses the original price and cannot
exceed reserved units. Subscription changes remain blocked by outstanding work,
including zero-cost reservations.

Deferred database guards bind reservation state to its immutable receipts and
ledger amounts. A ledger charge without a matching usage receipt is rejected,
even when its balance arithmetic is valid. Terminal reservations and quoted
prices cannot be rewritten. Indexed active holds and settlement dates support
admission and budget queries. The tenant dashboard displays actual consumed
units, reserved units, budget spending and reconciliation counts.

This is not the completed billing product. Payment orders/capture/refunds,
subscription invoices/tax, automatic renewals, the scheduled reservation sweeper,
and carrier/channel integration of the admission service remain unfinished.
Payment top-ups, auto-recharge and
channel activation remain unavailable. A funded account passes the setup guide's
funding review but does not establish provider or runtime readiness. Synthetic
verification adjustments are not production offers or payment evidence.
