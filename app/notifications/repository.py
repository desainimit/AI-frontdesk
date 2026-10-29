from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.notifications.models import OutboundMessage


class MessageRepository:
    """The clinic's outgoing messages, for code already inside its transaction."""

    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session = session
        self._tenant_id = context.tenant_id

    async def sender_number(
        self, provider_id: UUID | None = None, location_id: UUID | None = None
    ) -> str | None:
        """The clinic's number a text is sent from, when it has one: the provider's own
        line, else the location's, else the clinic's main number."""
        from app.calls.numbers import sender_for

        return await sender_for(self._session, self._tenant_id, provider_id, location_id)

    async def enqueue(
        self,
        *,
        channel: str,
        kind: str,
        recipient: str,
        body: str,
        dedupe_key: str,
        subject: str | None = None,
        sender: str | None = None,
        available_at: datetime | None = None,
        appointment_id: UUID | None = None,
    ) -> UUID | None:
        """Queue a message once: the same `dedupe_key` again changes nothing.

        Returns the queued message's ID, or nothing when it was already queued.
        """
        values: dict[str, object] = {
            "tenant_id": self._tenant_id,
            "channel": channel,
            "kind": kind,
            "recipient": recipient,
            "sender": sender,
            "subject": subject,
            "body": body,
            "appointment_id": appointment_id,
            "dedupe_key": dedupe_key,
        }
        if available_at is not None:
            values["available_at"] = available_at
        if channel == "sms":
            from app.settings.store import load_settings

            service = (await load_settings(self._session, self._tenant_id)).notifications
            values["messaging_service"] = service.messaging_service_sid or None
        return await self._session.scalar(
            insert(OutboundMessage)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_outbound_messages_dedupe")
            .returning(OutboundMessage.id)
        )

    async def cancel_for_appointment(self, appointment_id: UUID) -> None:
        """Stop what has not gone out yet for an appointment that changed."""
        await self._session.execute(
            update(OutboundMessage)
            .where(
                OutboundMessage.tenant_id == self._tenant_id,
                OutboundMessage.appointment_id == appointment_id,
                OutboundMessage.status == "queued",
            )
            .values(status="cancelled")
        )

    async def cancel_texts_to(self, recipient: str) -> None:
        """A patient who opted out of texts gets none of the ones still waiting."""
        await self._session.execute(
            update(OutboundMessage)
            .where(
                OutboundMessage.tenant_id == self._tenant_id,
                OutboundMessage.channel == "sms",
                OutboundMessage.recipient == recipient,
                OutboundMessage.status == "queued",
            )
            .values(status="cancelled")
        )

    async def recent(
        self, limit: int, offset: int, *, status: str | None = None, channel: str | None = None
    ) -> Sequence[OutboundMessage]:
        query = select(OutboundMessage).where(OutboundMessage.tenant_id == self._tenant_id)
        if status is not None:
            query = query.where(OutboundMessage.status == status)
        if channel is not None:
            query = query.where(OutboundMessage.channel == channel)
        return (
            await self._session.scalars(
                query.order_by(OutboundMessage.created_at.desc(), OutboundMessage.id)
                .limit(limit)
                .offset(offset)
            )
        ).all()
