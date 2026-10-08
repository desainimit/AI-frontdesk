import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.platform.mfa import consume_code, totp
from app.platform.models import PlatformAuthenticator
from app.security.encryption import SecretCipher


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


def test_factor_replay_lockout_and_recovery() -> None:
    user = uuid4()
    encryption = SecretCipher(
        SecretStr(json.dumps({"test": base64.b64encode(b"k" * 32).decode()})), "test"
    )
    secret = base64.b32encode(b"s" * 20).decode()
    key_id, ciphertext = encryption.encrypt(secret.encode(), f"platform-mfa:{user}")
    now = datetime(2026, 10, 8, tzinfo=UTC)
    counter = int(now.timestamp()) // 30
    factor = PlatformAuthenticator(
        user_id=user,
        key_id=key_id,
        ciphertext=ciphertext,
        last_counter=-1,
        failed_attempts=0,
        expires_at=now + timedelta(minutes=10),
    )
    code = totp(secret, counter)
    assert consume_code(factor, encryption, code, now)
    assert not consume_code(factor, encryption, code, now)
    for _ in range(4):
        assert not consume_code(factor, encryption, "badcode", now)
    assert factor.locked_until == now + timedelta(minutes=15)
    assert not consume_code(
        factor, encryption, totp(secret, counter + 1), now + timedelta(seconds=30)
    )
    later = now + timedelta(minutes=16)
    assert consume_code(factor, encryption, totp(secret, int(later.timestamp()) // 30), later)
