from typing import Any

from sqlalchemy import Boolean, CheckConstraint, String, text, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base
from app.core.database.types import IdentityMixin, TimestampMixin


class Tenant(IdentityMixin, TimestampMixin, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "human_transfer_number IS NULL OR human_transfer_number ~ '^\\+[1-9][0-9]{7,14}$'",
            name="human_transfer_e164",
        ),
        CheckConstraint("service_state IN ('setup', 'live')", name="service_state_allowed"),
    )

    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    human_transfer_number: Mapped[str | None] = mapped_column(String(16))
    # Independent of is_active: a setup workspace is administrable but takes no calls.
    service_state: Mapped[str] = mapped_column(String(20), default="setup", server_default="setup")
    # Only operator bootstrap may retain the old token-based activation contract.
    requires_saas_activation: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )

    language_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
    verification_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False
    )
