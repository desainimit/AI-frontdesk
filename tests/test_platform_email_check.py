import smtplib
from typing import Any

import pytest

from app.platform import connections
from app.platform.connections import PROBLEMS

CONFIG = {
    "host": "mail.example.test",
    "port": 587,
    "tls_mode": "plain",
    "username": "clinic@example.test",
    "sender": "clinic@example.test",
}


def server(*, offers_starttls: bool) -> type[smtplib.SMTP]:
    """An email server that refuses every sign-in made without encryption."""

    class Server(smtplib.SMTP):
        def __init__(self, *_: Any, **__: Any) -> None:
            super().__init__()
            self.encrypted = False

        def ehlo_or_helo_if_needed(self) -> None:
            self.does_esmtp = True
            self.esmtp_features = {"auth": "PLAIN LOGIN"}
            if offers_starttls and not self.encrypted:
                self.esmtp_features["starttls"] = ""

        def starttls(self, *_: Any, **__: Any) -> tuple[int, bytes]:
            self.encrypted = True
            return 220, b"ready"

        def login(self, *_: Any, **__: Any) -> tuple[int, bytes]:
            if not self.encrypted:
                raise smtplib.SMTPAuthenticationError(535, b"rejected")
            return 235, b"accepted"

        def noop(self) -> tuple[int, bytes]:
            return 250, b"ok"

        def close(self) -> None:
            return None

        def quit(self) -> tuple[int, bytes]:
            return 221, b"bye"

    return Server


def test_a_sign_in_refused_without_encryption_says_to_turn_encryption_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", server(offers_starttls=True))

    refused = connections._check_smtp(CONFIG, "secret")  # pyright: ignore[reportPrivateUsage]
    assert (refused.outcome, refused.code) == ("invalid", "encryption_required")
    assert "STARTTLS" in PROBLEMS[refused.code]

    encrypted = connections._check_smtp(  # pyright: ignore[reportPrivateUsage]
        {**CONFIG, "tls_mode": "starttls"}, "secret"
    )
    assert encrypted.outcome == "verified"


def test_a_server_with_no_encryption_to_offer_reports_the_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", server(offers_starttls=False))

    refused = connections._check_smtp(CONFIG, "secret")  # pyright: ignore[reportPrivateUsage]
    assert (refused.outcome, refused.code) == ("invalid", "credentials_rejected")
