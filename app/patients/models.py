from datetime import date

from sqlalchemy import Date, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.domain import TenantRecord


class Patient(TenantRecord):
    __tablename__ = "patients"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_patients_tenant_id_id"),
        UniqueConstraint("tenant_id", "account_identifier", name="uq_patients_account_identifier"),
    )
    display_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(16))
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    account_identifier: Mapped[str | None] = mapped_column(String(64))
