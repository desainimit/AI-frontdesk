from datetime import datetime
from typing import Any
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class Conversation(TenantRecord):
    """One thread with one person: text messages with a patient, or a test chat.

    A patient's number has one thread per clinic, like a messaging app. The assistant's
    working memory for the thread is kept here between messages.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id"),
        CheckConstraint("channel IN ('sms', 'test')", name="channel_allowed"),
        CheckConstraint("handler IN ('assistant', 'staff')", name="handler_allowed"),
        CheckConstraint("status IN ('open', 'closed')", name="status_allowed"),
        Index(
            "uq_conversations_contact",
            "tenant_id",
            "contact",
            unique=True,
            postgresql_where="channel = 'sms'",
        ),
        Index("ix_conversations_recent", "tenant_id", "last_message_at"),
        # Threads with a patient message the assistant has not answered yet.
        Index(
            "ix_conversations_pending",
            "pending_since",
            postgresql_where="pending_since IS NOT NULL",
        ),
    )
    channel: Mapped[str] = mapped_column(String(10))
    # The other person's phone number; empty for a test chat.
    contact: Mapped[str | None] = mapped_column(String(16))
    # The clinic's number the thread runs on.
    clinic_number: Mapped[str | None] = mapped_column(String(16))
    patient_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(String(10), default="open", server_default="open")
    # Who answers the next message: the assistant, or a staff member who took over.
    handler: Mapped[str] = mapped_column(
        String(10), default="assistant", server_default="assistant"
    )
    assigned_user_id: Mapped[UUID | None]
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_preview: Mapped[str] = mapped_column(String(160), default="", server_default="")
    # Patient messages no staff member has opened yet.
    unread: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    pending_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationMessage(TenantRecord):
    __tablename__ = "conversation_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_conversation_messages_conversation",
            ondelete="CASCADE",
        ),
        # The provider delivers the same message more than once; it is stored once.
        UniqueConstraint("tenant_id", "provider_message_id", name="uq_conversation_messages_sid"),
        CheckConstraint(
            "author IN ('patient', 'assistant', 'staff', 'system')", name="author_allowed"
        ),
        Index("ix_conversation_messages_thread", "conversation_id", "created_at"),
    )
    conversation_id: Mapped[UUID]
    author: Mapped[str] = mapped_column(String(10))
    author_user_id: Mapped[UUID | None]
    body: Mapped[str] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(64))
    # The queued text behind a message we sent; its delivery status lives there.
    outbound_id: Mapped[UUID | None]
