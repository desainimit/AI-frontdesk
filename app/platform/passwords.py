import hashlib
import hmac
import secrets

# OWASP scrypt CPU/RAM tradeoff: N=2^14, r=8, p=5 (16 MiB).
PREFIX = "scrypt$16384$8$5"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=5, dklen=32)
    return f"{PREFIX}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if f"{algorithm}${n}${r}${p}" != PREFIX:
            return False
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=5, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# Unknown accounts do the same expensive work as existing accounts.
DUMMY_HASH = f"{PREFIX}${'00' * 16}${'00' * 32}"
