"""Sign-in building blocks that need no database: tokens, passwords, codes, input rules."""

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from app.core.errors import AuthenticationError
from app.core.security.encryption import SecretCipher
from app.core.settings import Settings
from app.identity.models import AccountMfa
from app.identity.passwords import DUMMY_HASH, hash_password, verify_password
from app.identity.schemas import AcceptInvitation, DisableMfa, Login, MfaProof, NewPassword
from app.identity.tokens import (
    INVITATION,
    RESET,
    SESSION,
    digest_token,
    issue_token,
    parse_token,
    token_matches,
)
from app.identity.totp import (
    consume_code,
    consume_recovery,
    encrypt_secret,
    issue_recovery,
    provisioning_uri,
    qr_image,
    totp,
)
from app.main import create_app
from app.platform.bootstrap import BootstrapSettings
from tests.fakes import FakeDependency


def test_tokens_are_random_stored_as_digests_and_bound_to_their_kind() -> None:
    identity, token, digest = issue_token(SESSION)
    assert parse_token(token, SESSION) == identity
    assert digest == digest_token(token) and token not in digest
    assert issue_token(SESSION)[2] != digest
    assert token_matches(token, digest)
    assert not token_matches(token, None) and not token_matches(token + "x", digest)
    # A link of one kind is never accepted as another.
    for other in (INVITATION, RESET):
        with pytest.raises(AuthenticationError):
            parse_token(token, other)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "invalid",
        "fs1.bad.secret",
        f"fs1.{uuid4()}.short",
        f"fs1.{uuid4()}.{'a' * 43}.x",
        "x" * 200,
    ],
)
def test_malformed_tokens_are_refused(token: str) -> None:
    with pytest.raises(AuthenticationError):
        parse_token(token, SESSION)


def test_password_hashes_are_salted_and_fail_closed() -> None:
    password = "Test-only secure passphrase!"
    first, second = hash_password(password), hash_password(password)
    assert first != second and password not in first
    assert verify_password(password, first)
    assert not verify_password(password + "x", first)
    assert not verify_password(password, "corrupt")
    # The stand-in used for unknown accounts never matches anything.
    assert not verify_password(password, DUMMY_HASH)


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_sha1_vectors(instant: int, expected: str) -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp(secret, instant // 30, digits=8) == expected


def _factor(now: datetime) -> tuple[AccountMfa, SecretCipher, str]:
    encryption = SecretCipher(
        SecretStr(json.dumps({"test": base64.b64encode(b"k" * 32).decode()})), "test"
    )
    secret = base64.b32encode(b"s" * 20).decode()
    factor = AccountMfa(
        account_id=uuid4(),
        key_id="",
        ciphertext=b"",
        last_counter=-1,
        failed_attempts=0,
        expires_at=now + timedelta(minutes=10),
        recovery_digests=[],
        recovery_revision=0,
    )
    encrypt_secret(encryption, factor, secret)
    return factor, encryption, secret


def test_codes_work_once_and_repeated_failures_lock_for_a_while() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    factor, encryption, secret = _factor(now)
    counter = int(now.timestamp()) // 30
    code = totp(secret, counter)
    assert consume_code(factor, encryption, code, now)
    assert not consume_code(factor, encryption, code, now)  # replay
    for _ in range(4):
        assert not consume_code(factor, encryption, "badcode", now)
    assert factor.locked_until == now + timedelta(minutes=15)
    # A correct code is still refused while locked.
    assert not consume_code(
        factor, encryption, totp(secret, counter + 1), now + timedelta(seconds=30)
    )
    later = now + timedelta(minutes=16)
    assert consume_code(factor, encryption, totp(secret, int(later.timestamp()) // 30), later)
    # The secret is bound to its account: it cannot be decrypted as another account's.
    factor.account_id = uuid4()
    with pytest.raises(Exception):  # noqa: B017 - the cipher's own error type
        consume_code(factor, encryption, code, later + timedelta(minutes=1))


def test_recovery_codes_are_single_use_and_share_the_lock() -> None:
    now = datetime(2026, 10, 8, tzinfo=UTC)
    factor, _, _ = _factor(now)
    codes = issue_recovery(factor)
    assert len(codes) == 8 == len(set(codes)) and factor.recovery_revision == 1
    assert all(code not in digest for code in codes for digest in factor.recovery_digests)
    assert consume_recovery(factor, codes[0].lower(), now)  # case and dashes are forgiven
    assert not consume_recovery(factor, codes[0], now)
    assert len(factor.recovery_digests) == 7
    for _ in range(4):
        assert not consume_recovery(factor, "FD-" + "0" * 32, now)
    assert factor.locked_until is not None
    assert not consume_recovery(factor, codes[1], now)
    assert consume_recovery(factor, codes[1], now + timedelta(minutes=16))
    # New codes replace every old one.
    replaced = issue_recovery(factor)
    assert not consume_recovery(factor, codes[2], now + timedelta(minutes=17))
    assert consume_recovery(factor, replaced[0], now + timedelta(minutes=17))


def test_setup_gives_a_scannable_image_and_a_standard_uri() -> None:
    uri = provisioning_uri("Owner@Clinic.test", "JBSWY3DPEHPK3PXP")
    parts = urlsplit(uri)
    assert parts.scheme == "otpauth" and parts.netloc == "totp"
    assert unquote(parts.path) == "/AI Frontdesk:Owner@Clinic.test"
    assert parse_qs(parts.query) == {
        "secret": ["JBSWY3DPEHPK3PXP"],
        "issuer": ["AI Frontdesk"],
        "algorithm": ["SHA1"],
        "digits": ["6"],
        "period": ["30"],
    }
    image = qr_image(uri)
    assert image.startswith("data:image/svg+xml;") and "JBSWY3DPEHPK3PXP" not in image


@pytest.mark.parametrize("email", [123, None, "bad", "a@b", ""])
def test_malformed_sign_in_email_is_a_validation_error(email: Any) -> None:
    with pytest.raises(ValidationError):
        Login.model_validate({"email": email, "password": "test"})


def test_sign_in_input_is_normalised_and_never_echoed() -> None:
    command = Login.model_validate({"email": "  Owner@Clinic.TEST ", "password": "private-value"})
    assert command.email == "owner@clinic.test"
    assert "private-value" not in repr(command)
    with pytest.raises(ValidationError) as caught:
        Login.model_validate({"email": "owner@clinic.test", "password": "", "x": "private-value"})
    assert "private-value" not in str(caught.value)


def test_two_step_proof_is_exactly_one_of_code_or_recovery_code() -> None:
    recovery = "FD-" + "-".join(["ABCD"] * 8)
    assert MfaProof.model_validate({"code": "123456"}).recovery_code is None
    assert MfaProof.model_validate({"recovery_code": recovery}).code is None
    for invalid in ({}, {"code": "123456", "recovery_code": recovery}, {"code": "12345"}):
        with pytest.raises(ValidationError):
            MfaProof.model_validate(invalid)
    with pytest.raises(ValidationError):
        DisableMfa.model_validate({"code": "123456"})  # the password is required too


def test_new_passwords_need_length_and_variety() -> None:
    assert NewPassword.model_validate({"password": "a sensible passphrase"})
    for weak in ("short", "aaaaaaaaaaaaaaaa", "password1234", "123456789012"):
        with pytest.raises(ValidationError):
            NewPassword.model_validate({"password": weak})
    # An existing account accepts an invitation without choosing a password.
    assert AcceptInvitation.model_validate({"token": "fi1.x"}).password is None
    with pytest.raises(ValidationError):
        AcceptInvitation.model_validate({"token": "fi1.x", "password": "short"})


def test_bootstrap_requires_both_credentials_and_masks_password() -> None:
    empty = BootstrapSettings(super_admin_email="", super_admin_password=SecretStr(""))
    assert empty.command() is None
    with pytest.raises(ValidationError):
        BootstrapSettings(
            super_admin_email="admin@example.test", super_admin_password=SecretStr("")
        ).command()
    with pytest.raises(ValidationError):
        BootstrapSettings(super_admin_email="", super_admin_password=SecretStr("a" * 20)).command()
    configured = BootstrapSettings(
        super_admin_email=" ADMIN@example.test ",
        super_admin_password=SecretStr("private-seed-password"),
    )
    command = configured.command()
    assert command and command.email == "admin@example.test"
    assert "private-seed-password" not in repr(command)
    assert "private-seed-password" not in configured.model_dump_json()
    with pytest.raises(ValidationError):
        BootstrapSettings(
            super_admin_email="admin@example.test", super_admin_password=SecretStr("a" * 7)
        ).command()
    assert BootstrapSettings(
        super_admin_email="admin@example.test", super_admin_password=SecretStr("a" * 8)
    ).command()


def test_sign_in_is_refused_rather_than_left_unthrottled(settings: Settings) -> None:
    # Rate limits on, but Redis is a stand-in: guesses could not be counted.
    app = create_app(settings, redis_factory=lambda _: FakeDependency())
    with TestClient(app) as client:
        for path, body in (
            ("/api/v1/auth/login", {"email": "owner@clinic.test", "password": "x"}),
            ("/api/v1/auth/password/forgot", {"email": "owner@clinic.test"}),
            (
                "/api/v1/auth/password/reset",
                {"token": "fr1.x", "password": "a sensible passphrase"},
            ),
            ("/api/v1/auth/invitations/details", {"token": "fi1.x"}),
        ):
            response = client.post(path, json=body)
            assert response.status_code == 503, path
            assert response.json()["error"]["code"] == "authentication_unavailable"
            assert response.headers["cache-control"] == "no-store"
