from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin


class AiTurn(IdentityMixin, Base):
    """One reply of the assistant: what it cost, how fast it was and how it ended.

    Numbers and names only; never what was said.
    """

    __tablename__ = "ai_turns"
    __table_args__ = (
        CheckConstraint("channel IN ('voice', 'sms', 'test')", name="channel_allowed"),
        Index("ix_ai_turns_tenant_created", "tenant_id", "created_at"),
        Index("ix_ai_turns_call", "call_id"),
        Index("ix_ai_turns_conversation", "conversation_id"),
    )
    tenant_id: Mapped[UUID]
    channel: Mapped[str] = mapped_column(String(10))
    call_id: Mapped[UUID | None]
    conversation_id: Mapped[UUID | None]
    model: Mapped[str] = mapped_column(String(100), default="", server_default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Milliseconds until the first words were ready; empty when nothing was said.
    first_text_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    steps: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tools: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default="[]")
    # answered, confirmation, safety, handoff, unavailable, blocked, interrupted
    outcome: Mapped[str] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(String(40))
