from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class OutboundMessage(TenantRecord):
    """One message to a patient or a staff member, and what became of it.

    Written by the API in the same transaction as the booking it is about; sent by the
    worker. A reminder is simply a message whose `available_at` is in the future.
    """

    __tablename__ = "outbound_messages"
    __table_args__ = (
        # The same event never queues the same message twice, however often it is retried.
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_outbound_messages_dedupe"),
        ForeignKeyConstraint(
            ["tenant_id", "appointment_id"],
            ["appointments.tenant_id", "appointments.id"],
            name="fk_outbound_messages_appointment",
        ),
        CheckConstraint("channel IN ('sms', 'email')", name="channel_allowed"),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'cancelled')", name="status_allowed"
        ),
        Index("ix_outbound_messages_due", "available_at", postgresql_where="status = 'queued'"),
    )
    channel: Mapped[str] = mapped_column(String(10))
    # confirmation, reminder, rescheduled, cancellation or test
    kind: Mapped[str] = mapped_column(String(30))
    recipient: Mapped[str] = mapped_column(String(254))
    # The clinic's number an SMS is sent from; empty for email.
    sender: Mapped[str | None] = mapped_column(String(16))
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(10), default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A short code for why it could not be sent; never the provider's own text.
    last_error: Mapped[str | None] = mapped_column(String(40))
    appointment_id: Mapped[UUID | None]
    dedupe_key: Mapped[str] = mapped_column(String(120))
