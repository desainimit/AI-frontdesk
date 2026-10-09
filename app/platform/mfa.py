"""Encrypted authenticator enrollment, RFC 6238 validation, and replay prevention."""

import asyncio
import base64
import hashlib
import hmac
import secrets
import struct
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ApplicationError, AuthenticationError, ConflictError
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.platform.models import PlatformAuditEvent, PlatformAuthenticator, PlatformSession
from app.platform.passwords import verify_password
from app.platform.policies import PlatformContext
from app.platform.service import validate_context


class MfaUnavailableError(ApplicationError):
    status_code = 503
    code = "mfa_unavailable"
    public_message = "Authenticator encryption is not configured. Contact the platform operator."


class MfaCode(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    code: SecretStr = Field(min_length=6, max_length=6)


class RecoveryIssue(MfaCode):
    revision: int = Field(ge=0, strict=True)


class RecoveryReplace(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    password: SecretStr = Field(min_length=1, max_length=256)
    recovery_code: SecretStr = Field(min_length=32, max_length=64)


class RecoveryStatus(BaseModel):
    revision: int
    remaining: int
    enabled: bool


class RecoveryCodes(RecoveryStatus):
    codes: list[str] = Field(repr=False)


def recovery_digest(user_id: object, code: str) -> str:
    normalized = code.strip().upper().replace("-", "")
    return hashlib.sha256(
        f"frontdesk-platform-recovery:{user_id}:{normalized}".encode()
    ).hexdigest()


def issue_recovery(factor: PlatformAuthenticator) -> list[str]:
    codes = [
        "-".join(["FD", *[part[i : i + 4] for i in range(0, 32, 4)]])
        for part in [secrets.token_hex(16).upper() for _ in range(8)]
    ]
    factor.recovery_digests = [recovery_digest(factor.user_id, code) for code in codes]
    factor.recovery_revision += 1
    return codes


def consume_recovery(factor: PlatformAuthenticator, code: str, now: datetime) -> bool:
    if factor.locked_until and factor.locked_until > now:
        return False
    if factor.locked_until:
        factor.failed_attempts, factor.locked_until = 0, None
    digest = recovery_digest(factor.user_id, code)
    matches = [
        index
        for index, expected in enumerate(factor.recovery_digests)
        if hmac.compare_digest(digest, expected)
    ]
    if len(matches) == 1:
        factor.recovery_digests = [
            expected
            for index, expected in enumerate(factor.recovery_digests)
            if index != matches[0]
        ]
        factor.recovery_revision += 1
        factor.failed_attempts, factor.locked_until = 0, None
        return True
    factor.failed_attempts += 1
    if factor.failed_attempts >= 5:
        factor.locked_until = now + timedelta(minutes=15)
    return False


class EnrollmentView(BaseModel):
    secret: str = Field(repr=False)
    provisioning_uri: str = Field(repr=False)
    expires_at: datetime


def cipher(settings: Settings) -> SecretCipher:
    try:
        return SecretCipher(settings.secret_encryption_keys, settings.secret_encryption_active_key)
    except ValueError:
        raise MfaUnavailableError() from None


def totp(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret, casefold=False)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def consume_code(
    factor: PlatformAuthenticator, encryption: SecretCipher, code: str, now: datetime
) -> bool:
    if factor.locked_until and factor.locked_until > now:
        return False
    if factor.locked_until:
        factor.failed_attempts = 0
        factor.locked_until = None
    secret = encryption.decrypt(
        factor.key_id, factor.ciphertext, f"platform-mfa:{factor.user_id}"
    ).decode("ascii")
    current = int(now.timestamp()) // 30
    # One previous step tolerates delivery delay. Future codes and used codes are rejected.
    matched = next(
        (
            step
            for step in (current, current - 1)
            if code.isascii()
            and code.isdigit()
            and len(code) == 6
            and hmac.compare_digest(totp(secret, step), code)
        ),
        None,
    )
    if matched is not None and matched > factor.last_counter:
        factor.last_counter = matched
        factor.failed_attempts = 0
        factor.locked_until = None
        return True
    factor.failed_attempts += 1
    if factor.failed_attempts >= 5:
        factor.locked_until = now + timedelta(minutes=15)
    return False


class PlatformMfaService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.sessions, self.settings = sessions, settings

    async def begin(self, context: PlatformContext) -> EnrollmentView:
        encryption = cipher(self.settings)
        async with self.sessions.begin() as session:
            user = await validate_context(session, context, require_mfa=False)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": int.from_bytes(context.user_id.bytes[:8], "big", signed=True),
                },
            )
            factor = await session.scalar(
                select(PlatformAuthenticator)
                .where(PlatformAuthenticator.user_id == context.user_id)
                .with_for_update()
            )
            if factor is not None and factor.enabled_at is not None:
                raise ConflictError()
            secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
            key_id, ciphertext = encryption.encrypt(secret.encode(), f"platform-mfa:{user.id}")
            expiry = datetime.now(UTC) + timedelta(minutes=10)
            if factor is None:
                factor = PlatformAuthenticator(
                    user_id=user.id, key_id=key_id, ciphertext=ciphertext, expires_at=expiry
                )
                session.add(factor)
            else:
                factor.key_id, factor.ciphertext, factor.expires_at = key_id, ciphertext, expiry
                factor.failed_attempts, factor.last_counter, factor.locked_until = 0, -1, None
            session.add(
                PlatformAuditEvent(
                    user_id=user.id,
                    request_id=context.request_id,
                    action="platform_mfa_enrollment_started",
                )
            )
            uri = (
                "otpauth://totp/"
                + quote("AI Frontdesk:" + user.email, safe="")
                + "?"
                + urlencode(
                    {
                        "secret": secret,
                        "issuer": "AI Frontdesk",
                        "algorithm": "SHA1",
                        "digits": 6,
                        "period": 30,
                    }
                )
            )
            return EnrollmentView(secret=secret, provisioning_uri=uri, expires_at=expiry)

    async def confirm(self, context: PlatformContext, command: MfaCode) -> RecoveryCodes:
        encryption = cipher(self.settings)
        success = False
        result: RecoveryCodes | None = None
        async with self.sessions.begin() as session:
            await validate_context(session, context, require_mfa=False)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {
                    "key": int.from_bytes(context.user_id.bytes[:8], "big", signed=True),
                },
            )
            factor = await session.scalar(
                select(PlatformAuthenticator)
                .where(PlatformAuthenticator.user_id == context.user_id)
                .with_for_update()
            )
            now = datetime.now(UTC)
            if factor is not None and factor.enabled_at is None and factor.expires_at > now:
                success = consume_code(factor, encryption, command.code.get_secret_value(), now)
                if success:
                    factor.enabled_at = now
                    credential = await session.get(PlatformSession, context.session_id)
                    if credential is None:
                        raise AuthenticationError()
                    credential.mfa_verified_at = now
                    codes = issue_recovery(factor)
                    result = RecoveryCodes(
                        revision=factor.recovery_revision,
                        remaining=len(codes),
                        enabled=True,
                        codes=codes,
                    )
                    session.add(
                        PlatformAuditEvent(
                            user_id=context.user_id,
                            request_id=context.request_id,
                            action="platform_mfa_enrolled",
                        )
                    )
        # Commit attempts and lockout before returning authentication failure.
        if not success or result is None:
            raise AuthenticationError()
        return result

    async def recovery_status(self, context: PlatformContext) -> RecoveryStatus:
        async with self.sessions.begin() as session:
            await validate_context(session, context)
            factor = await session.scalar(
                select(PlatformAuthenticator).where(
                    PlatformAuthenticator.user_id == context.user_id
                )
            )
            if factor is None:
                raise AuthenticationError()
            return RecoveryStatus(
                revision=factor.recovery_revision,
                remaining=len(factor.recovery_digests),
                enabled=factor.enabled_at is not None,
            )

    async def recovery_issue(
        self, context: PlatformContext, command: RecoveryIssue
    ) -> RecoveryCodes:
        result: RecoveryCodes | None = None
        async with self.sessions.begin() as session:
            await validate_context(session, context)
            factor = await session.scalar(
                select(PlatformAuthenticator)
                .where(PlatformAuthenticator.user_id == context.user_id)
                .with_for_update()
            )
            await validate_context(session, context)
            if factor is None or factor.enabled_at is None:
                raise AuthenticationError()
            if factor.recovery_revision != command.revision:
                raise ConflictError()
            if consume_code(
                factor, cipher(self.settings), command.code.get_secret_value(), datetime.now(UTC)
            ):
                codes = issue_recovery(factor)
                result = RecoveryCodes(
                    revision=factor.recovery_revision,
                    remaining=len(codes),
                    enabled=True,
                    codes=codes,
                )
                session.add(
                    PlatformAuditEvent(
                        user_id=context.user_id,
                        request_id=context.request_id,
                        action="platform_recovery_codes_rotated",
                    )
                )
        if result is None:
            raise AuthenticationError()
        return result

    async def replace_factor(self, context: PlatformContext, command: RecoveryReplace) -> None:
        success = False
        async with self.sessions.begin() as session:
            user = await validate_context(session, context)
            factor = await session.scalar(
                select(PlatformAuthenticator)
                .where(PlatformAuthenticator.user_id == context.user_id)
                .with_for_update()
            )
            await validate_context(session, context)
            if factor is None or factor.enabled_at is None:
                raise AuthenticationError()
            valid_password = await asyncio.to_thread(
                verify_password, command.password.get_secret_value(), user.password_hash
            )
            if valid_password:
                await validate_context(session, context)
                now = datetime.now(UTC)
                success = consume_recovery(factor, command.recovery_code.get_secret_value(), now)
                if success:
                    factor.enabled_at = None
                    factor.expires_at = now
                    factor.recovery_digests = []
                    factor.recovery_revision += 1
                    await session.execute(
                        update(PlatformSession)
                        .where(
                            PlatformSession.user_id == user.id,
                            PlatformSession.id != context.session_id,
                            PlatformSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                    )
                    await session.execute(
                        update(PlatformSession)
                        .where(PlatformSession.id == context.session_id)
                        .values(mfa_verified_at=None)
                    )
                    session.add(
                        PlatformAuditEvent(
                            user_id=user.id,
                            request_id=context.request_id,
                            action="platform_authenticator_replacement_authorized",
                        )
                    )
            else:
                # An incorrect password must not provide an unthrottled factor-reset oracle.
                consume_recovery(factor, "", datetime.now(UTC))
        if not success:
            raise AuthenticationError()
