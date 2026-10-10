"""What a clinic and the platform can see and do about outgoing messages."""

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import ConflictError
from app.core.settings import Settings
from app.identity.sessions import platform_scope
from app.notifications.models import OutboundMessage
from app.notifications.templates import SMS_LIMIT, mask, render, when
from app.platform.policies import PlatformContext, PlatformPermission, require_platform_permission
from app.tenants.models import Tenant

Status = Literal["queued", "sending", "sent", "failed", "cancelled"]
# Why a message was not sent, in words for the delivery log.
PROBLEMS = {
    "not_configured": "Nothing to send it with yet: the email server or the clinic's phone "
    "number is not set up.",
    "provider_rejected": "It was refused. Check the phone number or email address.",
    "provider_unavailable": "The provider could not be reached.",
    "provider_timeout": "The provider did not answer in time.",
    "provider_rate_limited": "The provider asked us to slow down.",
    "too_late": "It could not be delivered in time, so it was not sent.",
}


class NoClinicNumberError(ConflictError):
    code = "no_clinic_number"
    public_message = "This clinic has no phone number to send texts from yet."


class MailNotReadyError(ConflictError):
    code = "mail_not_ready"
    public_message = "Email is not set up on this platform yet."


class MessageView(BaseModel):
    id: UUID
    channel: Literal["sms", "email"]
    kind: str
    # Masked: enough to recognise, not enough to copy.
    recipient: str
    status: Status
    created_at: datetime
    # When it is due; later than `created_at` for a reminder.
    available_at: datetime
    sent_at: datetime | None
    problem: str | None
    # Only in the platform's log.
    clinic: str | None = None


class TestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    channel: Literal["sms", "email"]
    to: str = Field(max_length=254)

    @model_validator(mode="after")
    def valid_recipient(self) -> "TestMessage":
        import re

        pattern = (
            r"\+[1-9][0-9]{7,14}" if self.channel == "sms" else r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+"
        )
        if not re.fullmatch(pattern, self.to):
            raise ValueError(
                "Use a full phone number with country code"
                if self.channel == "sms"
                else "Use a valid email address"
            )
        return self


def view(row: OutboundMessage, clinic: str | None = None) -> MessageView:
    return MessageView(
        id=row.id,
        channel=row.channel,  # pyright: ignore[reportArgumentType]
        kind=row.kind,
        recipient=mask(row.recipient),
        status=row.status,  # pyright: ignore[reportArgumentType]
        created_at=row.created_at,
        available_at=row.available_at,
        sent_at=row.sent_at,
        problem=PROBLEMS.get(row.last_error, "It could not be sent.") if row.last_error else None,
        clinic=clinic,
    )


class NotificationService:
    def __init__(self, uow: UnitOfWork, settings: Settings) -> None:
        self._uow, self._settings = uow, settings

    async def log(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        status: Status | None = None,
        channel: Literal["sms", "email"] | None = None,
    ) -> list[MessageView]:
        require_permission(context, Permission.SETTINGS_READ)
        async with self._uow.transaction(context) as tx:
            rows = await tx.messages.recent(limit, offset, status=status, channel=channel)
            return [view(row) for row in rows]

    async def send_test(self, context: RequestContext, command: TestMessage) -> MessageView:
        """Send the clinic's confirmation wording, with sample details, to one address."""
        require_permission(context, Permission.SETTINGS_UPDATE)
        async with self._uow.transaction(context) as tx:
            settings = await tx.schedule.settings()
            sender = await tx.messages.sender_number()
            if command.channel == "sms" and sender is None:
                raise NoClinicNumberError()
            if command.channel == "email" and not self._settings.mail_enabled:
                raise MailNotReadyError()
            body = render(
                settings.notifications.confirmation_text,
                {
                    "patient_name": "Alex",
                    "clinic_name": settings.clinic.name,
                    "provider_name": "Dr. Sample",
                    "service_name": "New patient visit",
                    "starts_at": when(datetime.now().astimezone(), settings.clinic.timezone),
                },
            )
            key = f"test:{uuid4()}"
            await tx.messages.enqueue(
                channel=command.channel,
                kind="test",
                recipient=command.to,
                sender=sender if command.channel == "sms" else None,
                subject=f"Test message from {settings.clinic.name}"
                if command.channel == "email"
                else None,
                body=f"[Test] {body}"[:SMS_LIMIT] if command.channel == "sms" else f"[Test] {body}",
                dedupe_key=key,
            )
            row = (await tx.messages.recent(1, 0))[0]
            return view(row)


class PlatformMessageLog:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def log(
        self, context: PlatformContext, limit: int, offset: int, *, status: Status | None = None
    ) -> list[MessageView]:
        """Messages of every clinic, newest first."""
        require_platform_permission(context, PlatformPermission.TENANT_READ)
        async with self._sessions.begin() as session:
            await platform_scope(session, context)
            query = select(OutboundMessage, Tenant.name).join(
                Tenant, Tenant.id == OutboundMessage.tenant_id
            )
            if status is not None:
                query = query.where(OutboundMessage.status == status)
            rows = await session.execute(
                query.order_by(OutboundMessage.created_at.desc(), OutboundMessage.id)
                .limit(limit)
                .offset(offset)
            )
            return [view(row, name) for row, name in rows]
