from uuid import UUID

from app.audit.events import AuditAction
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission, require_permission
from app.database.unit_of_work import UnitOfWork
from app.errors import AuthenticationError, NotFoundError
from app.users.schemas import UserView


class UserService:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._uow = unit_of_work

    async def me(self, context: RequestContext) -> UserView:
        if context.actor_type != ActorType.USER or context.actor_id is None:
            raise AuthenticationError()
        async with self._uow.transaction(context) as transaction:
            user = await transaction.users.get(context.actor_id)
            if user is None:
                raise NotFoundError()
            AuditService(transaction.audit).record(
                context, AuditAction.USER_VIEWED, "user", user.id
            )
            return UserView.model_validate(user).model_copy(
                update={"role_name": context.role_name or ""}
            )

    async def list(
        self,
        context: RequestContext,
        limit: int,
        offset: int,
        *,
        q: str | None = None,
        role: str | None = None,
        role_id: UUID | None = None,
        is_active: bool | None = None,
    ) -> list[UserView]:
        require_permission(context, Permission.USER_READ)
        async with self._uow.transaction(context) as transaction:
            users = await transaction.users.list(
                limit, offset, q=q, role=role, role_id=role_id, is_active=is_active
            )
            names = await transaction.users.role_names()
            accepted = await transaction.users.with_password([user.id for user in users])
            AuditService(transaction.audit).record(context, AuditAction.USERS_VIEWED, "users")
            views = [UserView.model_validate(user) for user in users]
            for view in views:
                view.role_name = names.get(view.role_id, "") if view.role_id else ""
                if not view.is_active:
                    view.status = "deactivated" if view.id in accepted else "invited"
            return views
