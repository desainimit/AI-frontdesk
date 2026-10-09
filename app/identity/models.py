from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin, TimestampMixin


class Account(IdentityMixin, TimestampMixin, Base):
    """One person, one email address, one password, for every workspace and the platform."""

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="normalized_email"),
        CheckConstraint("credential_revision >= 1", name="credential_revision_positive"),
    )
    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    # Empty until the owner accepts an invitation and chooses a password.
    password_hash: Mapped[str | None] = mapped_column(String(256))
    # Raised whenever the password changes; sessions issued before it stop working.
    credential_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    # Set only by the setup job; the API login cannot change it.
    # No Python-side default: an insert by the API login must not name this column.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, server_default=false())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())


class AccountMfa(IdentityMixin, Base):
    """An authenticator app for one account, with its single-use recovery codes."""

    __tablename__ = "account_mfa"
    __table_args__ = (
        CheckConstraint("recovery_revision >= 0", name="recovery_revision_nonnegative"),
        CheckConstraint(
            "jsonb_typeof(recovery_digests) = 'array' "
            "AND jsonb_array_length(recovery_digests) <= 8",
            name="recovery_codes_bounded",
        ),
    )
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), unique=True)
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    # Empty while the owner is still scanning the code; two-step is on once set.
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_counter: Mapped[int] = mapped_column(BigInteger, default=-1)
    failed_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # An unfinished setup is discarded after this time.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recovery_digests: Mapped[list[str]] = mapped_column(
        JSONB, default=list[str], server_default="[]"
    )
    recovery_revision: Mapped[int] = mapped_column(default=0, server_default="0")


class AccountSession(IdentityMixin, Base):
    """A signed-in browser. The only credential the API accepts."""

    __tablename__ = "sessions"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64))
    credential_revision: Mapped[int]
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # While true the two-step code is still owed and the session opens nothing else.
    mfa_pending: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    mfa_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The workspace this session is working in. Chosen on the server, never sent by the browser.
    tenant_id: Mapped[UUID | None] = mapped_column(ForeignKey("tenants.id"), index=True)
    # A platform administrator inside a workspace: one identifier per visit, why, until when.
    support_id: Mapped[UUID | None]
    support_reason: Mapped[str | None] = mapped_column(String(500))
    support_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthChallenge(IdentityMixin, Base):
    """A single-use emailed link: an invitation to a workspace, or a password reset."""

    __tablename__ = "auth_challenges"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        UniqueConstraint("key_digest", name="uq_auth_challenges_key"),
        CheckConstraint("purpose IN ('invitation', 'reset')", name="challenge_purpose"),
        CheckConstraint(
            "purpose <> 'invitation' OR (tenant_id IS NOT NULL AND user_id IS NOT NULL)",
            name="invitation_membership",
        ),
    )
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), index=True)
    # Set for an invitation: the membership it activates.
    tenant_id: Mapped[UUID | None]
    user_id: Mapped[UUID | None]
    purpose: Mapped[str] = mapped_column(String(20))
    # Makes a repeated request return the same challenge instead of sending twice.
    key_digest: Mapped[str] = mapped_column(String(64))
    token_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A reset link stops working if the password changed after it was sent.
    credential_revision: Mapped[int | None]


class AuthMail(IdentityMixin, Base):
    """The encrypted email for one challenge, queued for the worker and erased once sent."""

    __tablename__ = "auth_mail"
    __table_args__ = (
        UniqueConstraint("challenge_id", name="uq_auth_mail_challenge"),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'cancelled')", name="mail_status"
        ),
    )
    challenge_id: Mapped[UUID] = mapped_column(ForeignKey("auth_challenges.id"))
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    lease_id: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
