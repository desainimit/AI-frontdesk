"""Authenticator-app codes (RFC 6238) and single-use recovery codes for one account."""

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

import segno

from app.core.errors import ApplicationError
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.identity.models import AccountMfa

ISSUER = "AI Frontdesk"
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15


class MfaUnavailableError(ApplicationError):
    status_code = 503
    code = "mfa_unavailable"
    public_message = "Two-step verification is not available. Contact the platform operator."


def cipher(settings: Settings) -> SecretCipher:
    try:
        return SecretCipher(settings.secret_encryption_keys, settings.secret_encryption_active_key)
    except ValueError:
        raise MfaUnavailableError() from None


def _context(factor: AccountMfa) -> str:
    return f"account-mfa:{factor.account_id}"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def encrypt_secret(encryption: SecretCipher, factor: AccountMfa, secret: str) -> None:
    factor.key_id, factor.ciphertext = encryption.encrypt(secret.encode(), _context(factor))


def provisioning_uri(email: str, secret: str) -> str:
    """What an authenticator app reads from the QR code."""
    query = urlencode(
        {"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": 6, "period": 30}
    )
    return "otpauth://totp/" + quote(f"{ISSUER}:{email}", safe="") + "?" + query


def qr_image(uri: str) -> str:
    """The provisioning URI as an SVG data URI, ready to use as an image source."""
    return segno.make(uri, error="m").svg_data_uri(scale=5, border=2, dark="#000", light="#fff")


def totp(secret: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret, casefold=False)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def _unlock_if_due(factor: AccountMfa, now: datetime) -> bool:
    """False while the factor is locked after repeated failures."""
    if factor.locked_until and factor.locked_until > now:
        return False
    if factor.locked_until:
        factor.failed_attempts, factor.locked_until = 0, None
    return True


def _failed(factor: AccountMfa, now: datetime) -> bool:
    factor.failed_attempts += 1
    if factor.failed_attempts >= MAX_FAILED_ATTEMPTS:
        factor.locked_until = now + timedelta(minutes=LOCK_MINUTES)
    return False


def consume_code(factor: AccountMfa, encryption: SecretCipher, code: str, now: datetime) -> bool:
    """Accept a current code once. Failures count towards a temporary lock."""
    if not _unlock_if_due(factor, now):
        return False
    secret = encryption.decrypt(factor.key_id, factor.ciphertext, _context(factor)).decode("ascii")
    current = int(now.timestamp()) // 30
    well_formed = code.isascii() and code.isdigit() and len(code) == 6
    # One previous step tolerates a slow typist. Future and already used codes are refused.
    matched = next(
        (
            step
            for step in (current, current - 1)
            if well_formed and hmac.compare_digest(totp(secret, step), code)
        ),
        None,
    )
    if matched is not None and matched > factor.last_counter:
        factor.last_counter = matched
        factor.failed_attempts, factor.locked_until = 0, None
        return True
    return _failed(factor, now)


def recovery_digest(account_id: object, code: str) -> str:
    normalized = code.strip().upper().replace("-", "")
    return hashlib.sha256(f"frontdesk-recovery:{account_id}:{normalized}".encode()).hexdigest()


def issue_recovery(factor: AccountMfa) -> list[str]:
    """Replace every recovery code with eight new ones; only their digests are stored."""
    codes = [
        "-".join(["FD", *[part[i : i + 4] for i in range(0, 32, 4)]])
        for part in [secrets.token_hex(16).upper() for _ in range(8)]
    ]
    factor.recovery_digests = [recovery_digest(factor.account_id, code) for code in codes]
    factor.recovery_revision += 1
    return codes


def consume_recovery(factor: AccountMfa, code: str, now: datetime) -> bool:
    """Accept one unused recovery code and remove it."""
    if not _unlock_if_due(factor, now):
        return False
    digest = recovery_digest(factor.account_id, code)
    matches = [
        index
        for index, expected in enumerate(factor.recovery_digests)
        if hmac.compare_digest(digest, expected)
    ]
    if len(matches) != 1:
        return _failed(factor, now)
    factor.recovery_digests = [
        expected for index, expected in enumerate(factor.recovery_digests) if index != matches[0]
    ]
    factor.recovery_revision += 1
    factor.failed_attempts, factor.locked_until = 0, None
    return True
