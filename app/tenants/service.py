from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.core.database.unit_of_work import UnitOfWork
from app.core.errors import NotFoundError
from app.tenants.schemas import TenantView


class TenantService:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._uow = unit_of_work

    async def current(self, context: RequestContext) -> TenantView:
        require_permission(context, Permission.TENANT_READ)
        async with self._uow.transaction(context) as transaction:
            tenant = await transaction.tenants.current()
            if tenant is None:
                raise NotFoundError()
            return TenantView.model_validate(tenant)
