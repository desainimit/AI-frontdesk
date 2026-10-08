"""SMTP delivery for encrypted invitation/reset messages using the restricted worker role.

Drained by app.worker. SMTP is at-least-once; retries reuse the same message ID and
challenge, and never mint another valid token/code.
"""

import asyncio
import json
import logging
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.human_models import AuthMail
from app.config.settings import Settings
from app.events.store import verify_worker_role
from app.security.encryption import SecretCipher

logger = logging.getLogger(__name__)


def send_smtp(settings: Settings, payload: dict[str, str], identity: str) -> None:
    if not settings.smtp_enabled:
        raise ValueError("SMTP is disabled")
    message = EmailMessage()
    message["From"] = settings.smtp_sender
    message["To"] = payload["to"]
    message["Subject"] = payload["subject"]
    # Stable per message, so a retried delivery is recognisable as the same email.
    message["Message-ID"] = f"<{identity}@{settings.smtp_sender.rsplit('@', 1)[-1]}>"
    message.set_content(payload["body"])
    context = ssl.create_default_context()
    if settings.smtp_tls_mode == "tls":
        connection = smtplib.SMTP_SSL(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
            context=context,
        )
    else:
        connection = smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
        )
    with connection:
        if settings.smtp_tls_mode == "starttls":
            connection.starttls(context=context)
        if settings.smtp_username:
            connection.login(settings.smtp_username, settings.smtp_password.get_secret_value())
        refused = connection.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


async def deliver_one(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> bool:
    now = datetime.now(UTC)
    lease_id = uuid4()
    async with sessions.begin() as session:
        await verify_worker_role(session)
        row = await session.scalar(
            select(AuthMail)
            .where(
                or_(
                    (AuthMail.status == "queued") & (AuthMail.available_at <= now),
                    (AuthMail.status == "sending") & (AuthMail.lease_until < now),
                )
            )
            .order_by(AuthMail.available_at, AuthMail.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False
        if row.expires_at <= now or row.attempts >= 5 or row.ciphertext is None:
            row.status, row.ciphertext = "failed", None
            return True
        row.status, row.lease_id = "sending", lease_id
        row.lease_until = now + timedelta(seconds=120)
        row.attempts += 1
        identity, tenant_id, key_id, ciphertext = row.id, row.tenant_id, row.key_id, row.ciphertext
    delivered = False
    try:
        cipher = SecretCipher(
            settings.secret_encryption_keys, settings.secret_encryption_active_key
        )
        payload = json.loads(
            cipher.decrypt(key_id, ciphertext, f"auth-mail:{tenant_id}:{identity}")
        )
        await asyncio.to_thread(send_smtp, settings, payload, str(identity))
        delivered = True
    except Exception:
        # SMTP and crypto exception messages can contain recipients or credentials.
        logger.warning("auth_mail_delivery_failed", extra={"event_id": str(identity)})
    async with sessions.begin() as session:
        row = await session.scalar(
            select(AuthMail)
            .where(
                AuthMail.id == identity, AuthMail.lease_id == lease_id, AuthMail.status == "sending"
            )
            .with_for_update()
        )
        if row is not None:
            if delivered:
                row.status, row.ciphertext = "sent", None
            elif row.attempts >= 5 or row.expires_at <= datetime.now(UTC):
                row.status, row.ciphertext = "failed", None
            else:
                row.status = "queued"
                row.available_at = datetime.now(UTC) + timedelta(seconds=30 * 2**row.attempts)
            row.lease_id, row.lease_until = None, None
    return True
