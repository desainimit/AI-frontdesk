"""Two-step verification for an account: set up, turn on, turn off, new recovery codes.

Optional for every account. Once on, each sign-in needs a code from the authenticator
app, or one of eight single-use recovery codes when the phone is unavailable.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database.rls import verify_runtime_role
from app.core.errors import AuthenticationError, ConflictError
from app.core.settings import Settings
from app.identity.models import Account, AccountMfa
from app.identity.passwords import DUMMY_HASH, verify_password
from app.identity.schemas import DisableMfa, MfaCode, MfaSetupView, RecoveryCodesView
from app.identity.service import record, revoke_sessions
from app.identity.sessions import load_session
from app.identity.totp import (
    cipher,
    consume_code,
    consume_recovery,
    encrypt_secret,
    issue_recovery,
    new_secret,
    provisioning_uri,
    qr_image,
)

SETUP_WINDOW = timedelta(minutes=10)


async def _factor(db: AsyncSession, account_id: UUID) -> AccountMfa | None:
    # One setup or change at a time per account, including when no row exists yet.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": int.from_bytes(account_id.bytes[:8], "big", signed=True)},
    )
    return await db.scalar(
        select(AccountMfa).where(AccountMfa.account_id == account_id).with_for_update()
    )


class MfaService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sessions, self._settings = sessions, settings

    async def setup(self, token: str, request_id: UUID) -> MfaSetupView:
        """Start (or restart) setup: a new secret to scan, valid for ten minutes."""
        encryption = cipher(self._settings)
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            _, account = await load_session(db, token)
            factor = await _factor(db, account.id)
            if factor is not None and factor.enabled_at is not None:
                raise ConflictError()
            secret = new_secret()
            expires = datetime.now(UTC) + SETUP_WINDOW
            if factor is None:
                factor = AccountMfa(account_id=account.id, key_id="", ciphertext=b"")
                db.add(factor)
            factor.expires_at = expires
            factor.failed_attempts, factor.last_counter, factor.locked_until = 0, -1, None
            encrypt_secret(encryption, factor, secret)
            await record(db, account.id, "two_step_setup_started", request_id)
            uri = provisioning_uri(account.email, secret)
            return MfaSetupView(
                secret=secret, provisioning_uri=uri, qr_image=qr_image(uri), expires_at=expires
            )

    async def enable(self, token: str, command: MfaCode, request_id: UUID) -> RecoveryCodesView:
        """Confirm the app works with one code, turn two-step on and hand out recovery codes."""
        encryption = cipher(self._settings)
        codes: list[str] | None = None
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            row, account = await load_session(db, token, lock=True)
            factor = await _factor(db, account.id)
            now = datetime.now(UTC)
            pending = factor is not None and factor.enabled_at is None and factor.expires_at > now
            if (
                factor is not None
                and pending
                and consume_code(factor, encryption, command.code.get_secret_value(), now)
            ):
                factor.enabled_at = row.mfa_verified_at = now
                codes = issue_recovery(factor)
                # Other browsers signed in with the password alone must sign in again.
                await revoke_sessions(db, account.id, keep=row.id)
                await record(db, account.id, "two_step_enabled", request_id)
        # A wrong code is counted and committed before the error is raised.
        if codes is None:
            raise AuthenticationError()
        return RecoveryCodesView(codes=codes)

    async def recovery_codes(
        self, token: str, command: MfaCode, request_id: UUID
    ) -> RecoveryCodesView:
        """Replace all recovery codes; needs a current code from the app."""
        codes: list[str] | None = None
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            _, account = await load_session(db, token)
            factor = await _factor(db, account.id)
            if factor is None or factor.enabled_at is None:
                raise ConflictError()
            if consume_code(
                factor, cipher(self._settings), command.code.get_secret_value(), datetime.now(UTC)
            ):
                codes = issue_recovery(factor)
                await record(db, account.id, "recovery_codes_replaced", request_id)
        if codes is None:
            raise AuthenticationError()
        return RecoveryCodesView(codes=codes)

    async def disable(self, token: str, command: DisableMfa, request_id: UUID) -> None:
        """Turn two-step off; needs the password and a code (or a recovery code)."""
        async with self._sessions.begin() as db:
            await verify_runtime_role(db)
            _, account = await load_session(db, token)
            expected = account.password_hash or DUMMY_HASH
        valid_password = await asyncio.to_thread(
            verify_password, command.password.get_secret_value(), expected
        )
        done = False
        async with self._sessions.begin() as db:
            row, _ = await load_session(db, token, lock=True)
            account = await db.scalar(
                select(Account).where(Account.id == row.account_id).with_for_update()
            )
            factor = await _factor(db, row.account_id)
            if account is None or factor is None or factor.enabled_at is None:
                raise ConflictError()
            now = datetime.now(UTC)
            if not valid_password or account.password_hash != expected:
                # A wrong password must not give unlimited guesses at the code.
                consume_recovery(factor, "", now)
            elif (
                consume_recovery(factor, command.recovery_code.get_secret_value(), now)
                if command.recovery_code is not None
                else consume_code(
                    factor,
                    cipher(self._settings),
                    command.code.get_secret_value() if command.code else "",
                    now,
                )
            ):
                await db.delete(factor)
                row.mfa_verified_at = None
                await record(db, account.id, "two_step_disabled", request_id)
                done = True
        if not done:
            raise AuthenticationError()
