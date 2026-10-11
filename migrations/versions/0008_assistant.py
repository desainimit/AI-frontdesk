"""Conversations with patients, tickets, and what each reply of the assistant cost.

Tickets replace the follow-up queue. Calls remember the number they came from.

Revision ID: 0008_assistant
Revises: 0007_calendar_sync
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_assistant"
down_revision: str | None = "0007_calendar_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = ("ai_turns", "conversations", "conversation_messages", "tickets", "ticket_events")


def upgrade() -> None:
    op.create_table(
        "ai_turns",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.String(length=100), server_default="", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_text_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("steps", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "tools", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("error", sa.String(length=40), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('voice', 'sms', 'test')", name=op.f("ck_ai_turns_channel_allowed")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_turns")),
    )
    op.create_index("ix_ai_turns_call", "ai_turns", ["call_id"], unique=False)
    op.create_index("ix_ai_turns_conversation", "ai_turns", ["conversation_id"], unique=False)
    op.create_index(
        "ix_ai_turns_tenant_created", "ai_turns", ["tenant_id", "created_at"], unique=False
    )
    op.create_table(
        "conversations",
        sa.Column("channel", sa.String(length=10), nullable=False),
        sa.Column("contact", sa.String(length=16), nullable=True),
        sa.Column("clinic_number", sa.String(length=16), nullable=True),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="open", nullable=False),
        sa.Column("handler", sa.String(length=10), server_default="assistant", nullable=False),
        sa.Column("assigned_user_id", sa.Uuid(), nullable=True),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_preview", sa.String(length=160), server_default="", nullable=False),
        sa.Column("unread", sa.Integer(), server_default="0", nullable=False),
        sa.Column("pending_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('sms', 'test')", name=op.f("ck_conversations_channel_allowed")
        ),
        sa.CheckConstraint(
            "handler IN ('assistant', 'staff')", name=op.f("ck_conversations_handler_allowed")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed')", name=op.f("ck_conversations_status_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_conversations_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_conversations_tenant_id")),
    )
    op.create_index(
        "ix_conversations_pending",
        "conversations",
        ["pending_since"],
        unique=False,
        postgresql_where="pending_since IS NOT NULL",
    )
    op.create_index(
        "ix_conversations_recent", "conversations", ["tenant_id", "last_message_at"], unique=False
    )
    op.create_index(
        op.f("ix_conversations_tenant_id"), "conversations", ["tenant_id"], unique=False
    )
    op.create_index(
        "uq_conversations_contact",
        "conversations",
        ["tenant_id", "contact"],
        unique=True,
        postgresql_where="channel = 'sms'",
    )
    op.create_table(
        "tickets",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("category", sa.String(length=60), server_default="", nullable=False),
        sa.Column("priority", sa.String(length=10), server_default="normal", nullable=False),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("contact", sa.String(length=16), nullable=True),
        sa.Column("call_id", sa.Uuid(), nullable=True),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("appointment_id", sa.Uuid(), nullable=True),
        sa.Column("assignee_id", sa.Uuid(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(length=120), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "priority IN ('low', 'normal', 'high', 'urgent')",
            name=op.f("ck_tickets_priority_allowed"),
        ),
        sa.CheckConstraint(
            "source IN ('call', 'sms', 'manual', 'calendar', 'assistant')",
            name=op.f("ck_tickets_source_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting', 'resolved')",
            name=op.f("ck_tickets_status_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_tickets_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name=op.f("uq_tickets_dedupe")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_tickets_tenant_id")),
        sa.UniqueConstraint("tenant_id", "number", name=op.f("uq_tickets_number")),
    )
    op.create_index(
        "ix_tickets_open", "tickets", ["tenant_id", "status", "created_at"], unique=False
    )
    op.create_index(op.f("ix_tickets_tenant_id"), "tickets", ["tenant_id"], unique=False)
    op.create_table(
        "conversation_messages",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("author", sa.String(length=10), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=64), nullable=True),
        sa.Column("outbound_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "author IN ('patient', 'assistant', 'staff', 'system')",
            name=op.f("ck_conversation_messages_author_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name=op.f("fk_conversation_messages_conversation"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_conversation_messages_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_messages")),
        sa.UniqueConstraint(
            "tenant_id", "provider_message_id", name=op.f("uq_conversation_messages_sid")
        ),
    )
    op.create_index(
        op.f("ix_conversation_messages_tenant_id"),
        "conversation_messages",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_messages_thread",
        "conversation_messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "ticket_events",
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("author", sa.String(length=10), server_default="system", nullable=False),
        sa.Column("outbound_id", sa.Uuid(), nullable=True),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('created', 'note', 'reply', 'status', 'assignment', 'priority')",
            name=op.f("ck_ticket_events_kind_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "ticket_id"],
            ["tickets.tenant_id", "tickets.id"],
            name=op.f("fk_ticket_events_ticket"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name=op.f("fk_ticket_events_tenant_id_tenants")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_events")),
    )
    op.create_index(
        op.f("ix_ticket_events_tenant_id"), "ticket_events", ["tenant_id"], unique=False
    )
    op.create_index(
        "ix_ticket_events_ticket", "ticket_events", ["ticket_id", "created_at"], unique=False
    )
    op.drop_table("follow_up_events")
    op.add_column("calls", sa.Column("caller_number", sa.String(length=16), nullable=True))
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )


def downgrade() -> None:
    op.drop_column("calls", "caller_number")
    for table in ("ticket_events", "conversation_messages", "tickets", "conversations", "ai_turns"):
        op.drop_table(table)
