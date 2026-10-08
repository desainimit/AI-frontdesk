from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import RequestContext
from app.auth.policies import Permission, require_permission
from app.calls.verification import VerificationPolicy
from app.conversation.locales import LanguagePolicy
from app.database.unit_of_work import UnitOfWork
from app.errors import NotFoundError
from app.tenants.schemas import TenantName, TenantView, TransferDestination


class TenantService:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._uow = unit_of_work

    async def current(self, context: RequestContext, default_language: str = "en") -> TenantView:
        require_permission(context, Permission.TENANT_READ)
        async with self._uow.transaction(context) as transaction:
            tenant = await transaction.tenants.current()
            if tenant is None:
                raise NotFoundError()
            view = TenantView.model_validate(tenant)
            if not tenant.language_policy:
                view.language_policy = LanguagePolicy(default_language=default_language)
            return view

    async def rename(self, context: RequestContext, command: TenantName) -> TenantView:
        require_permission(context, Permission.TENANT_UPDATE)
        async with self._uow.transaction(context) as transaction:
            await transaction.tenants.require_legacy_configuration()
            tenant = await transaction.tenants.current()
            if tenant is None:
                raise NotFoundError()
            if tenant.name != command.name:
                tenant.name = command.name
                AuditService(transaction.audit).record(
                    context, AuditAction.TENANT_RENAMED, "tenant", tenant.id
                )
            return TenantView.model_validate(tenant)

    async def set_language_policy(
        self, context: RequestContext, command: LanguagePolicy
    ) -> TenantView:
        require_permission(context, Permission.TENANT_UPDATE)
        async with self._uow.transaction(context) as transaction:
            await transaction.tenants.require_legacy_configuration()
            tenant = await transaction.tenants.current()
            if tenant is None:
                raise NotFoundError()
            tenant.language_policy = command.model_dump(mode="json")
            AuditService(transaction.audit).record(
                context, AuditAction.TENANT_LANGUAGES_UPDATED, "tenant", tenant.id
            )
            return TenantView.model_validate(tenant)

    async def set_transfer_destination(
        self, context: RequestContext, command: TransferDestination
    ) -> TenantView:
        require_permission(context, Permission.TENANT_UPDATE)
        async with self._uow.transaction(context) as transaction:
            await transaction.tenants.require_legacy_configuration()
            tenant = await transaction.tenants.current()
            if tenant is None:
                raise NotFoundError()
            tenant.human_transfer_number = command.number
            AuditService(transaction.audit).record(
                context, AuditAction.TENANT_TRANSFER_UPDATED, "tenant", tenant.id
            )
            return TenantView.model_validate(tenant)

    async def set_verification_policy(
        self, context: RequestContext, command: VerificationPolicy
    ) -> TenantView:
        require_permission(context, Permission.TENANT_UPDATE)
        async with self._uow.transaction(context) as transaction:
            tenant = await transaction.tenants.current()
            if tenant is None:
                raise NotFoundError()
            tenant.verification_policy = command.model_dump(mode="json")
            await transaction.tenants.revoke_call_access()
            AuditService(transaction.audit).record(
                context, AuditAction.TENANT_VERIFICATION_UPDATED, "tenant", tenant.id
            )
            return TenantView.model_validate(tenant)
