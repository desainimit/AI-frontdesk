"""Opaque expiring keyset cursors bound to the actor, authorization and exact query."""

import base64
import json
from datetime import UTC, datetime

from cryptography.exceptions import InvalidTag
from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.core.errors import ConflictError
from app.core.security.encryption import SecretCipher


class CursorAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    identity: str
    revision: int
    expires: int


class CursorCodec:
    def __init__(self, cipher: SecretCipher, binding: str) -> None:
        self.cipher = cipher
        self.binding = "list-cursor-v1:" + binding

    def encode(self, name: str, identity: str, revision: int) -> str:
        anchor = CursorAnchor(
            name=name,
            identity=identity,
            revision=revision,
            expires=int(datetime.now(UTC).timestamp()) + 900,
        )
        key, ciphertext = self.cipher.encrypt(anchor.model_dump_json().encode(), self.binding)
        return base64.urlsafe_b64encode(
            json.dumps([key, base64.b64encode(ciphertext).decode()]).encode()
        ).decode()

    def decode(self, token: str) -> CursorAnchor:
        try:
            key, encoded = (
                RootModel[tuple[str, str]]
                .model_validate_json(
                    base64.b64decode(token, altchars=b"-_", validate=True), strict=True
                )
                .root
            )
            anchor = CursorAnchor.model_validate_json(
                self.cipher.decrypt(key, base64.b64decode(encoded, validate=True), self.binding)
            )
            if anchor.expires <= int(datetime.now(UTC).timestamp()):
                raise ValueError()
            return anchor
        except (ValueError, TypeError, InvalidTag):
            raise ConflictError() from None
