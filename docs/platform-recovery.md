# Platform authenticator recovery

Enrollment returns eight 128-bit random recovery codes once. The database stores
SHA-256 digests bound to the platform user. No API retrieves plaintext codes.
The design uses high-entropy, hashed, throttled, single-use proofs described in
[NIST's saved recovery-code guidance](https://pages.nist.gov/800-63-4/sp800-63b.html).
This implementation is not a claim of NIST certification or complete compliance.

Sign in using the password and one unused code. Recovery sign-in atomically consumes
that code and revokes older platform sessions, including their tenant-entry contexts.
The account-security page reports remaining codes without returning their values.

To replace a lost authenticator, confirm the current password and another unused
code. This invalidates every old recovery code, revokes other sessions and removes
the current session's MFA verification. Enroll the replacement before performing
platform administration. The new enrollment returns a fresh set of codes.

Rotation requires a fresh, unused authenticator code and the current recovery
revision. It invalidates the old set atomically. Wrong proofs share the factor's
five-attempt, fifteen-minute lockout. Replays and concurrent consumption fail.
Recovery activity is attributed in immutable platform audit records.

If a write response is lost, check account/recovery status before another security
write. A lost rotation response cannot be recovered as plaintext; load the current
revision, then explicitly generate a replacement set using another fresh TOTP.
The dashboard preserves protection when an outcome check also fails.

Migration 0032 adds an empty recovery set to existing authenticators. Existing
administrators generate their first set through Account security. No migration
changes their authenticator or password. Legacy confirmation remains supported;
the dashboard uses the confirmation endpoint that returns recovery codes.

Total loss of the password or every factor is not recoverable through an unauthenticated
reset endpoint. A separately approved, identity-verified operator recovery process
and operational security-notification delivery remain release work.
