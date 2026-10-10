from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class CalendarConnection(TenantRecord):
    """A provider's own Google or Outlook calendar, connected with their consent.

    Only the refresh token is kept, as ciphertext. Access tokens are fetched when
    needed and never stored.
    """

    __tablename__ = "calendar_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_calendar_connections_tenant_id_id"),
        UniqueConstraint("tenant_id", "provider_id", name="uq_calendar_connections_provider"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["providers.tenant_id", "providers.id"],
            name="fk_calendar_connections_provider",
        ),
        CheckConstraint("kind IN ('google', 'microsoft')", name="kind_allowed"),
        CheckConstraint("status IN ('connected', 'needs_attention')", name="status_allowed"),
    )
    provider_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(10))
    # The calendar account, shown so staff can tell which calendar is connected.
    account_email: Mapped[str] = mapped_column(String(254))
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(20), default="connected")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(40))


class CalendarOAuthState(TenantRecord):
    """A sign-in at Google or Microsoft that has been started but not finished."""

    __tablename__ = "calendar_oauth_states"
    __table_args__ = (
        UniqueConstraint("state_digest", name="uq_calendar_oauth_states_state"),
        ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["providers.tenant_id", "providers.id"],
            name="fk_calendar_oauth_states_provider",
        ),
    )
    provider_id: Mapped[UUID]
    kind: Mapped[str] = mapped_column(String(10))
    state_digest: Mapped[str] = mapped_column(String(64))
    # PKCE: proves at the exchange that the same party started this sign-in.
    code_verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CalendarBusyBlock(TenantRecord):
    """Time a provider is busy in their own calendar. Replaced on every sync."""

    __tablename__ = "calendar_busy_blocks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["providers.tenant_id", "providers.id"],
            name="fk_calendar_busy_blocks_provider",
        ),
        CheckConstraint("ends_at > starts_at", name="positive_duration"),
        Index("ix_calendar_busy_blocks_window", "tenant_id", "provider_id", "starts_at"),
    )
    provider_id: Mapped[UUID]
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppointmentCalendarLink(TenantRecord):
    """The calendar event written for an appointment, so later changes find it again."""

    __tablename__ = "appointment_calendar_links"
    __table_args__ = (
        UniqueConstraint("tenant_id", "appointment_id", name="uq_appointment_calendar_links"),
        ForeignKeyConstraint(
            ["tenant_id", "appointment_id"],
            ["appointments.tenant_id", "appointments.id"],
            name="fk_appointment_calendar_links_appointment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["calendar_connections.tenant_id", "calendar_connections.id"],
            name="fk_appointment_calendar_links_connection",
            ondelete="CASCADE",
        ),
        CheckConstraint("status IN ('synced', 'conflict')", name="status_allowed"),
    )
    appointment_id: Mapped[UUID]
    connection_id: Mapped[UUID]
    external_event_id: Mapped[str] = mapped_column(String(1024))
    # `conflict`: the event was moved or deleted in the calendar; a person must decide.
    status: Mapped[str] = mapped_column(String(10), default="synced")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
