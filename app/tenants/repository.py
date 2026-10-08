from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import RequestContext
from app.calls.models import Call
from app.configuration.models import ConfigurationDraft
from app.errors import ConflictError
from app.tenants.models import Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession, context: RequestContext) -> None:
        self._session = session
        self._context = context

    async def current(self) -> Tenant | None:
        return await self._session.scalar(
            select(Tenant).where(Tenant.id == self._context.tenant_id)
        )

    async def revoke_call_access(self) -> None:
        await self._session.execute(
            update(Call)
            .where(Call.tenant_id == self._context.tenant_id, Call.status == "active")
            .values(
                access_patient_id=None,
                verified_patient_id=None,
                access_digest=None,
                access_salt=None,
                access_expires_at=None,
                verification_expires_at=None,
                access_attempts=0,
            )
        )

    async def require_legacy_configuration(self) -> None:
        # Serialize with configuration publication before testing its current pointer.
        await self._session.scalar(
            select(Tenant).where(Tenant.id == self._context.tenant_id).with_for_update()
        )
        version = await self._session.scalar(
            select(ConfigurationDraft.published_version).where(
                ConfigurationDraft.tenant_id == self._context.tenant_id
            )
        )
        if version is not None:
            raise ConflictError()
