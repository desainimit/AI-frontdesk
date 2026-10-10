"""The worker's side of notifications: take one due message and send it.

Email goes through the platform's email server. Texts go through the clinic's own phone
service connection when it has one, otherwise the platform's. A message that cannot be
sent is retried with growing pauses, then marked failed with a short reason.
"""

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.events.providers import (
    PermanentProviderError,
    ProviderError,
    RateLimitedProviderError,
    RetryPolicy,
    TransientProviderError,
)
from app.events.store import verify_worker_role
from app.identity.mail import send_smtp
from app.integrations.resolver import TenantIntegrationResolver
from app.notifications.models import OutboundMessage

logger = logging.getLogger(__name__)

LEASE_SECONDS = 120
SMS_TIMEOUT_SECONDS = 10
# A reminder that could not go out this long after it was due is no longer useful.
STALE_AFTER = timedelta(hours=6)


class NotConfiguredError(PermanentProviderError):
    """Nothing to send with: no email server, or no phone service for this clinic."""

    code = "not_configured"


@dataclass(frozen=True)
class _Claimed:
    id: UUID
    tenant_id: UUID
    channel: str
    recipient: str
    sender: str | None
    subject: str | None
    body: str
    attempts: int
    lease: UUID


class MessageDelivery:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        resolver: TenantIntegrationResolver,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._sessions, self._settings, self._resolver = sessions, settings, resolver
        self._transport = transport
        self._retry = retry or RetryPolicy(max_attempts=5, base_seconds=30, max_seconds=1800)

    async def _claim(self) -> tuple[bool, _Claimed | None]:
        """(whether a message was taken, the message unless it was settled without sending)."""
        now = datetime.now(UTC)
        async with self._sessions.begin() as session:
            await verify_worker_role(session)
            row = await session.scalar(
                select(OutboundMessage)
                .where(
                    or_(
                        (OutboundMessage.status == "queued")
                        & (OutboundMessage.available_at <= now),
                        (OutboundMessage.status == "sending") & (OutboundMessage.lease_until < now),
                    )
                )
                .order_by(OutboundMessage.available_at, OutboundMessage.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False, None
            if now - row.available_at > STALE_AFTER:
                row.status, row.last_error = "failed", "too_late"
                row.lease_token, row.lease_until = None, None
                return True, None
            lease = uuid4()
            row.status, row.lease_token = "sending", lease
            row.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            row.attempts += 1
            return True, _Claimed(
                row.id,
                row.tenant_id,
                row.channel,
                row.recipient,
                row.sender,
                row.subject,
                row.body,
                row.attempts,
                lease,
            )

    async def _send_email(self, message: _Claimed) -> None:
        if not self._settings.mail_enabled:
            raise NotConfiguredError()
        payload = {"to": message.recipient, "subject": message.subject or "", "body": message.body}
        try:
            await asyncio.to_thread(send_smtp, self._settings, payload, str(message.id))
        except smtplib.SMTPRecipientsRefused:
            raise PermanentProviderError() from None
        except (smtplib.SMTPException, OSError):
            raise TransientProviderError() from None

    async def _send_sms(self, message: _Claimed) -> None:
        connection = await self._resolver.active(message.tenant_id, "telephony")
        if connection is not None and connection.account_identifier:
            account = connection.account_identifier
            token = connection.secret("auth_token").get_secret_value()
        elif self._settings.platform_twilio:
            account = self._settings.twilio_account_sid
            token = self._settings.twilio_auth_token.get_secret_value()
        else:
            raise NotConfiguredError()
        if not message.sender:
            raise NotConfiguredError()
        try:
            async with httpx.AsyncClient(
                timeout=SMS_TIMEOUT_SECONDS, follow_redirects=False, transport=self._transport
            ) as client:
                response = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{account}/Messages.json",
                    data={"To": message.recipient, "From": message.sender, "Body": message.body},
                    auth=httpx.BasicAuth(account, token),
                )
        except httpx.HTTPError:
            raise TransientProviderError() from None
        if response.status_code == 429:
            raise RateLimitedProviderError()
        if response.status_code >= 500:
            raise TransientProviderError()
        if response.status_code not in {200, 201}:
            # Wrong number, a number that cannot receive texts, or rejected credentials.
            raise PermanentProviderError()

    async def deliver_one(self) -> bool:
        """Send one due message. False when nothing is waiting."""
        taken, message = await self._claim()
        if message is None:
            return taken
        failure: ProviderError | None = None
        try:
            if message.channel == "email":
                await self._send_email(message)
            else:
                await self._send_sms(message)
        except ProviderError as error:
            failure = error
        except Exception as error:
            logger.error("message_delivery_unexpected", extra={"error_type": type(error).__name__})
            failure = TransientProviderError()
        retry_in = self._retry.delay(message.attempts, failure) if failure else None
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(OutboundMessage)
                .where(
                    OutboundMessage.id == message.id,
                    OutboundMessage.lease_token == message.lease,
                    OutboundMessage.status == "sending",
                )
                .with_for_update()
            )
            if row is None:
                return True
            row.lease_token, row.lease_until = None, None
            if failure is None:
                row.status, row.sent_at, row.last_error = "sent", datetime.now(UTC), None
            elif retry_in is None:
                row.status, row.last_error = "failed", failure.code
            else:
                row.status, row.last_error = "queued", failure.code
                row.available_at = datetime.now(UTC) + timedelta(seconds=retry_in)
        if failure is not None:
            logger.warning(
                "message_delivery_failed",
                extra={"message_id": str(message.id), "error_code": failure.code},
            )
        return True
