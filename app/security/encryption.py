"""Authenticated database ciphertext bound to its tenant and purpose.

Keyring is deployment-injected JSON {key_id: base64_32_byte_key}; never stored in SQL.
"""

import base64
import hashlib
import hmac
import json
import secrets
from typing import cast

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr


class SecretCipher:
    def __init__(self, keyring: SecretStr, active_key: str) -> None:
        try:
            parsed: object = json.loads(keyring.get_secret_value())
            if not isinstance(parsed, dict):
                raise ValueError
            values = cast(dict[object, object], parsed)
            if not values or len(values) > 10:
                raise ValueError
            self._keys: dict[str, bytes] = {}
            for name, encoded in values.items():
                if (
                    not isinstance(name, str)
                    or not isinstance(encoded, str)
                    or not name
                    or len(name) > 64
                ):
                    raise ValueError
                key = base64.b64decode(encoded, validate=True)
                if len(key) != 32:
                    raise ValueError
                self._keys[name] = key
            if active_key not in self._keys:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Configure a valid encryption keyring and active key ID") from None
        self.active_key = active_key

    def encrypt(self, plaintext: bytes, context: str) -> tuple[str, bytes]:
        nonce = secrets.token_bytes(12)
        return self.active_key, nonce + AESGCM(self._keys[self.active_key]).encrypt(
            nonce, plaintext, context.encode()
        )

    def fingerprint(self, plaintext: bytes, context: str) -> str:
        """Keyed equality digest for retry detection; never an unkeyed hash of a secret.

        Uses a subkey derived for this purpose only, so it is not the encryption key.
        """
        subkey = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"frontdesk-fingerprint-v1"
        ).derive(self._keys[self.active_key])
        return hmac.new(subkey, context.encode() + b"\x00" + plaintext, hashlib.sha256).hexdigest()

    def decrypt(self, key_id: str, ciphertext: bytes, context: str) -> bytes:
        key = self._keys.get(key_id)
        if key is None or len(ciphertext) < 29:
            raise ValueError("Encrypted data is unavailable")
        return AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], context.encode())
