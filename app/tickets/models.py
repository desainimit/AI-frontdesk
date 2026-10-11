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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord

STATUSES = ("open", "in_progress", "waiting", "resolved")
PRIORITIES = ("low", "normal", "high", "urgent")
SOURCES = ("call", "sms", "manual", "calendar", "assistant")


class Ticket(TenantRecord):
    """A patient's request that a staff member has to deal with."""

    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tickets_tenant_id"),
        UniqueConstraint("tenant_id", "number", name="uq_tickets_number"),
        # One automatic ticket per cause, however often the cause is reported.
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_tickets_dedupe"),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting', 'resolved')", name="status_allowed"
        ),
        CheckConstraint("priority IN ('low', 'normal', 'high', 'urgent')", name="priority_allowed"),
        CheckConstraint(
            "source IN ('call', 'sms', 'manual', 'calendar', 'assistant')", name="source_allowed"
        ),
        Index("ix_tickets_open", "tenant_id", "status", "created_at"),
    )
    number: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    category: Mapped[str] = mapped_column(String(60), default="", server_default="")
    priority: Mapped[str] = mapped_column(String(10), default="normal", server_default="normal")
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open")
    source: Mapped[str] = mapped_column(String(10))
    patient_id: Mapped[UUID | None]
    # Where to reach the person when they are not a patient on record: a phone number.
    contact: Mapped[str | None] = mapped_column(String(16))
    call_id: Mapped[UUID | None]
    conversation_id: Mapped[UUID | None]
    appointment_id: Mapped[UUID | None]
    assignee_id: Mapped[UUID | None]
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedupe_key: Mapped[str | None] = mapped_column(String(120))


class TicketEvent(TenantRecord):
    """What happened on a ticket, in order: notes, replies and changes."""

    __tablename__ = "ticket_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            name="fk_ticket_events_ticket",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('created', 'note', 'reply', 'status', 'assignment', 'priority')",
            name="kind_allowed",
        ),
        Index("ix_ticket_events_ticket", "ticket_id", "created_at"),
    )
    ticket_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    # The staff member; empty when the assistant or the system did it.
    author_user_id: Mapped[UUID | None]
    author: Mapped[str] = mapped_column(String(10), default="system", server_default="system")
    # A reply to the patient is a queued message; its delivery status lives there.
    outbound_id: Mapped[UUID | None]
