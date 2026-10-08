from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import IdentityMixin


class HumanIdentity(IdentityMixin, Base):
    """One password identity shared by explicitly accepted tenant memberships."""

    __tablename__ = "human_identities"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="normalized_email"),
        CheckConstraint("credential_revision >= 1", name="credential_revision_positive"),
    )
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(256))
    credential_revision: Mapped[int] = mapped_column(default=1)


class HumanAccount(IdentityMixin, Base):
    __tablename__ = "human_accounts"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        UniqueConstraint("tenant_id", "user_id", name="uq_human_accounts_membership"),
        UniqueConstraint("tenant_id", "user_id", "identity_id", name="uq_human_accounts_identity"),
    )
    tenant_id: Mapped[UUID]
    user_id: Mapped[UUID]
    identity_id: Mapped[UUID] = mapped_column(ForeignKey("human_identities.id"))


class HumanSession(IdentityMixin, Base):
    __tablename__ = "human_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "identity_id"],
            ["human_accounts.tenant_id", "human_accounts.user_id", "human_accounts.identity_id"],
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    user_id: Mapped[UUID]
    identity_id: Mapped[UUID] = mapped_column(ForeignKey("human_identities.id"))
    credential_revision: Mapped[int]
    token_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuthChallenge(IdentityMixin, Base):
    __tablename__ = "auth_challenges"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"]),
        UniqueConstraint("tenant_id", "id", name="uq_auth_challenges_tenant_id_id"),
        UniqueConstraint("tenant_id", "key_digest", name="uq_auth_challenges_key"),
        CheckConstraint("purpose IN ('invitation', 'reset')", name="challenge_purpose"),
        CheckConstraint("attempts BETWEEN 0 AND 5", name="challenge_attempts"),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    user_id: Mapped[UUID]
    purpose: Mapped[str] = mapped_column(String(20))
    key_digest: Mapped[str] = mapped_column(String(64))
    token_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)
    credential_revision: Mapped[int | None]


class AuthMail(IdentityMixin, Base):
    __tablename__ = "auth_mail"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "challenge_id"], ["auth_challenges.tenant_id", "auth_challenges.id"]
        ),
        UniqueConstraint("challenge_id", name="uq_auth_mail_challenge"),
        CheckConstraint(
            "status IN ('queued', 'sending', 'sent', 'failed', 'cancelled')", name="mail_status"
        ),
    )
    tenant_id: Mapped[UUID] = mapped_column(index=True)
    challenge_id: Mapped[UUID]
    key_id: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    lease_id: Mapped[UUID | None]
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
