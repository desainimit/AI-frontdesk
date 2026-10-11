"""Restricted transcript snapshots and reports of finished calls."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin


class CallTranscript(IdentityMixin, Base):
    __tablename__ = "call_transcripts"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"]),
        UniqueConstraint("tenant_id", "call_id", name="uq_call_transcript"),
        Index("ix_call_transcripts_tenant_created", "tenant_id", "created_at"),
    )
    tenant_id: Mapped[UUID]
    call_id: Mapped[UUID]
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PostCallReport(IdentityMixin, Base):
    __tablename__ = "post_call_reports"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "call_id"], ["calls.tenant_id", "calls.id"]),
        UniqueConstraint("tenant_id", "call_id", name="uq_post_call_report"),
        CheckConstraint("turn_count BETWEEN 0 AND 80", name="turn_count_bounded"),
        Index("ix_post_call_reports_tenant_ended", "tenant_id", "call_ended_at"),
    )
    tenant_id: Mapped[UUID]
    call_id: Mapped[UUID]
    call_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    summary: Mapped[str] = mapped_column(String(1200), nullable=False)
    outcome: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    transcript_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_count: Mapped[int]
