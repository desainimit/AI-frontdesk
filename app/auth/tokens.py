import hashlib
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import SecretStr

from app.core.errors import AuthenticationError


@dataclass(frozen=True)
class CredentialLocator:
    tenant_id: UUID
    credential_id: UUID


def digest_token(token: str) -> str:
    # SHA-256 is appropriate for uniformly random 256-bit API keys, not human passwords.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(tenant_id: UUID) -> tuple[UUID, SecretStr, str]:
    credential_id = uuid4()
    token = f"fd1.{tenant_id}.{credential_id}.{secrets.token_urlsafe(32)}"
    return credential_id, SecretStr(token), digest_token(token)


def parse_locator(token: str) -> CredentialLocator:
    """IDs are untrusted lookup hints until the full token digest has been verified."""
    try:
        version, tenant, credential, secret = token.split(".")
        if version != "fd1" or len(secret) != 43 or len(token) > 160:
            raise ValueError
        return CredentialLocator(UUID(tenant), UUID(credential))
    except ValueError:
        raise AuthenticationError() from None
