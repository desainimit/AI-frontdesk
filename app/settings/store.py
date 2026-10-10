"""Reading and creating a clinic's settings row, for code already inside a transaction."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.settings.defaults import DEFAULT_TIMEZONE, default_settings
from app.settings.models import TenantSettings
from app.settings.schemas import ClinicConfiguration
from app.tenants.models import Tenant


async def seed_settings(
    session: AsyncSession, tenant: Tenant, timezone: str = DEFAULT_TIMEZONE
) -> TenantSettings:
    """Give a new clinic its starting settings. Does nothing when it already has them."""
    row = await session.scalar(select(TenantSettings).where(TenantSettings.tenant_id == tenant.id))
    if row is None:
        row = TenantSettings(
            tenant_id=tenant.id,
            version=1,
            document=default_settings(tenant.name, timezone).model_dump(mode="json"),
        )
        session.add(row)
        await session.flush()
    return row


async def load_settings(session: AsyncSession, tenant_id: UUID) -> ClinicConfiguration:
    """The clinic's current settings, inside a transaction already scoped to it."""
    document = await session.scalar(
        select(TenantSettings.document).where(TenantSettings.tenant_id == tenant_id)
    )
    if document is None:
        raise NotFoundError()
    return ClinicConfiguration.model_validate(document)
