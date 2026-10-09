"""Opaque bearer tokens: a lookup identifier plus 256 random bits, stored only as a digest."""

import hashlib
import hmac
import re
import secrets
from uuid import UUID, uuid4

from app.core.errors import AuthenticationError

SESSION = "fs1"
INVITATION = "fi1"
RESET = "fr1"


def digest_token(token: str) -> str:
    # SHA-256 suits uniformly random 256-bit tokens; it is not for human passwords.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(prefix: str) -> tuple[UUID, str, str]:
    """A new identifier, the token to hand out once, and the digest to store."""
    identity = uuid4()
    token = f"{prefix}.{identity}.{secrets.token_urlsafe(32)}"
    return identity, token, digest_token(token)


def parse_token(token: str, prefix: str) -> UUID:
    """The identifier is an untrusted lookup hint until the digest has been compared."""
    try:
        kind, identity, secret = token.split(".")
        if kind != prefix or not re.fullmatch(r"[A-Za-z0-9_-]{43}", secret) or len(token) > 100:
            raise ValueError
        return UUID(identity)
    except ValueError:
        raise AuthenticationError() from None


def token_matches(token: str, stored_digest: str | None) -> bool:
    """Constant-time comparison that does the same work when there is nothing stored."""
    return hmac.compare_digest(stored_digest or "0" * 64, digest_token(token))
