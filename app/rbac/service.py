from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID, uuid5

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.models import AuditEvent
from app.auth.context import ActorType, RequestContext
from app.auth.human_models import HumanAccount
from app.auth.policies import Permission, require_permission
from app.core.database.unit_of_work import enter_tenant_scope
from app.core.errors import AuthorizationError, ConflictError, NotFoundError
from app.rbac.catalog import CUSTODIAN_PERMISSIONS, template_permissions
from app.rbac.models import RolePermission, TenantRole
from app.rbac.schemas import PermissionGrant, RoleCommand, RoleOption, RoleView, UpdateRole
from app.tenants.models import Tenant
from app.users.models import User


class RevisionConflictError(ConflictError):
    status_code = 412
    code = "revision_conflict"
    public_message = "This resource changed. Reload the current revision and review your edits."


async def seed_roles(session: AsyncSession, tenant_id: UUID) -> dict[str, UUID]:
    """Called inside tenant creation, or once to bind legacy development accounts."""
    await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update())
    existing = (
        await session.scalars(select(TenantRole).where(TenantRole.tenant_id == tenant_id))
    ).all()
    roles = {row.template_key: row.id for row in existing if row.template_key}
    for key in ("owner", "admin", "operator", "analyst", "member"):
        if key in roles:
            continue
        identity = uuid5(tenant_id, f"role:{key}")
        session.add(
            TenantRole(
                id=identity,
                tenant_id=tenant_id,
                name=key.title(),
                description=f"Initial {key} template",
                template_key=key,
                revision=1,
            )
        )
        await session.flush()
        allowed = template_permissions(key)
        session.add_all(
            [
                RolePermission(
                    tenant_id=tenant_id,
                    role_id=identity,
                    key=item.value,
                    allowed=item.value in allowed,
                )
                for item in Permission
            ]
        )
        roles[key] = identity
    await session.flush()
    return roles


async def bind_user_permissions(
    session: AsyncSession, context: RequestContext, user: User, *, lock: bool = False
) -> RequestContext:
    if user.role_id is None:
        user.role_id = await legacy_role(session, user.tenant_id, user.role)
        await session.flush()
    query = select(TenantRole).where(
        TenantRole.id == user.role_id, TenantRole.tenant_id == user.tenant_id
    )
    if lock:
        query = query.with_for_update(read=True)
    role = await session.scalar(query)
    if role is None:
        raise AuthorizationError()
    grants = (
        await session.scalars(
            select(RolePermission.key).where(
                RolePermission.tenant_id == user.tenant_id,
                RolePermission.role_id == role.id,
                RolePermission.allowed.is_(True),
            )
        )
    ).all()
    known = {item.value for item in Permission}
    return replace(
        context,
        role_id=role.id,
        role_name=role.name,
        authorization_revision=role.revision,
        capabilities=frozenset(grants) & known,
    )


async def legacy_role(session: AsyncSession, tenant_id: UUID, key: str) -> UUID:
    identity = await session.scalar(
        select(TenantRole.id).where(
            TenantRole.tenant_id == tenant_id, TenantRole.template_key == key
        )
    )
    if identity is None:
        raise NotFoundError()
    return identity


def audit(
    session: AsyncSession,
    context: RequestContext,
    action: str,
    resource_type: str,
    resource_id: UUID | None = None,
) -> None:
    session.add(
        AuditEvent(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            actor_type=context.actor_type,
            platform_user_id=context.platform_user_id,
            support_session_id=context.support_session_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=context.request_id,
        )
    )


async def assert_custodian(session: AsyncSession, tenant_id: UUID) -> None:
    active = (
        await session.execute(
            select(User.role_id, RolePermission.key)
            .join(
                RolePermission,
                (RolePermission.role_id == User.role_id)
                & (RolePermission.tenant_id == User.tenant_id),
            )
            .join(Tenant, Tenant.id == User.tenant_id)
            .outerjoin(
                HumanAccount,
                and_(HumanAccount.tenant_id == User.tenant_id, HumanAccount.user_id == User.id),
            )
            .where(
                User.tenant_id == tenant_id,
                or_(
                    User.is_active.is_(True),
                    and_(Tenant.service_state == "setup", HumanAccount.id.is_(None)),
                ),
                RolePermission.allowed.is_(True),
            )
        )
    ).all()
    by_role: dict[UUID, set[str]] = {}
    for role_id, key in active:
        if role_id is not None:
            by_role.setdefault(role_id, set()).add(key)
    if not any({item.value for item in CUSTODIAN_PERMISSIONS} <= keys for keys in by_role.values()):
        raise ConflictError()


async def grantable_role(
    session: AsyncSession, context: RequestContext, identity: UUID
) -> TenantRole:
    role = await session.scalar(
        select(TenantRole).where(
            TenantRole.tenant_id == context.tenant_id, TenantRole.id == identity
        )
    )
    if role is None:
        raise NotFoundError()
    grants = set(
        await session.scalars(
            select(RolePermission.key).where(
                RolePermission.tenant_id == context.tenant_id,
                RolePermission.role_id == identity,
                RolePermission.allowed.is_(True),
            )
        )
    )
    if context.actor_type != ActorType.PLATFORM_SUPPORT and not grants <= context.capabilities:
        raise AuthorizationError()
    return role


class RoleService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def _enter(
        self,
        session: AsyncSession,
        context: RequestContext,
        permission: Permission,
        *,
        write: bool = False,
    ) -> None:
        require_permission(context, permission)
        if write:
            from app.core.database.rls import set_tenant_scope

            await set_tenant_scope(session, context.tenant_id)
            await session.scalar(
                select(Tenant.id).where(Tenant.id == context.tenant_id).with_for_update()
            )
        await enter_tenant_scope(session, context)

    async def _view(self, session: AsyncSession, role: TenantRole) -> RoleView:
        rows = (
            await session.execute(
                select(RolePermission.key, RolePermission.allowed).where(
                    RolePermission.tenant_id == role.tenant_id,
                    RolePermission.role_id == role.id,
                )
            )
        ).all()
        values = {key: allowed for key, allowed in rows}
        return RoleView(
            id=role.id,
            name=role.name,
            description=role.description,
            revision=role.revision,
            permissions=[
                PermissionGrant(
                    module=item.value.split(":")[0],
                    permission=item.value.split(":")[1],
                    allowed=values.get(item.value, False),
                )
                for item in Permission
            ],
        )

    async def list(self, context: RequestContext) -> list[RoleView]:
        async with self.sessions.begin() as session:
            await self._enter(session, context, Permission.ROLE_READ)
            rows = (
                await session.scalars(
                    select(TenantRole)
                    .where(TenantRole.tenant_id == context.tenant_id)
                    .order_by(TenantRole.name)
                )
            ).all()
            return [await self._view(session, row) for row in rows]

    async def assignable(self, context: RequestContext) -> Sequence[RoleOption]:
        permission = (
            Permission.ROLE_ASSIGN
            if Permission.ROLE_ASSIGN.value in context.capabilities
            else Permission.USER_INVITE
        )
        async with self.sessions.begin() as session:
            await self._enter(session, context, permission)
            roles = (
                await session.scalars(
                    select(TenantRole)
                    .where(TenantRole.tenant_id == context.tenant_id)
                    .order_by(TenantRole.name)
                )
            ).all()
            options: list[RoleOption] = []
            for role in roles:
                try:
                    await grantable_role(session, context, role.id)
                except AuthorizationError:
                    continue
                options.append(RoleOption(id=role.id, name=role.name))
            return options

    async def save(
        self, context: RequestContext, command: RoleCommand, identity: UUID | None = None
    ) -> RoleView:
        permission = Permission.ROLE_CREATE if identity is None else Permission.ROLE_UPDATE
        allowed = {grant.key for grant in command.permissions if grant.allowed}
        if context.actor_type != ActorType.PLATFORM_SUPPORT and not allowed <= context.capabilities:
            raise AuthorizationError()
        async with self.sessions.begin() as session:
            await self._enter(session, context, permission, write=True)
            duplicate = await session.scalar(
                select(TenantRole.id).where(
                    TenantRole.tenant_id == context.tenant_id,
                    TenantRole.name == command.name,
                    TenantRole.id != identity if identity else TenantRole.id.is_not(None),
                )
            )
            if duplicate:
                raise ConflictError()
            if identity is None:
                role = TenantRole(
                    tenant_id=context.tenant_id,
                    name=command.name,
                    description=command.description,
                    revision=1,
                )
                session.add(role)
                await session.flush()
            else:
                role = await session.scalar(
                    select(TenantRole)
                    .where(TenantRole.tenant_id == context.tenant_id, TenantRole.id == identity)
                    .with_for_update()
                )
                if role is None:
                    raise NotFoundError()
                if not isinstance(command, UpdateRole) or command.revision != role.revision:
                    raise RevisionConflictError()
                existing = {
                    grant.key
                    for grant in (await self._view(session, role)).permissions
                    if grant.allowed
                }
                if (
                    context.actor_type != ActorType.PLATFORM_SUPPORT
                    and not existing <= context.capabilities
                ):
                    raise AuthorizationError()
                role.name, role.description = command.name, command.description
                role.revision += 1
            await session.execute(
                delete(RolePermission).where(
                    RolePermission.tenant_id == context.tenant_id, RolePermission.role_id == role.id
                )
            )
            session.add_all(
                [
                    RolePermission(
                        tenant_id=context.tenant_id,
                        role_id=role.id,
                        key=item.value,
                        allowed=item.value in allowed,
                    )
                    for item in Permission
                ]
            )
            await session.flush()
            await assert_custodian(session, context.tenant_id)
            audit(
                session,
                context,
                "role_created" if identity is None else "role_updated",
                "role",
                role.id,
            )
            return await self._view(session, role)

    async def assign(self, context: RequestContext, user_id: UUID, role_id: UUID) -> None:
        async with self.sessions.begin() as session:
            await self._enter(session, context, Permission.ROLE_ASSIGN, write=True)
            user = await session.scalar(
                select(User)
                .where(User.id == user_id, User.tenant_id == context.tenant_id)
                .with_for_update()
            )
            role = await session.scalar(
                select(TenantRole).where(
                    TenantRole.id == role_id, TenantRole.tenant_id == context.tenant_id
                )
            )
            if user is None or role is None:
                raise NotFoundError()
            if user.role_id is not None:
                await grantable_role(session, context, user.role_id)
            grants = {
                grant.key
                for grant in (await self._view(session, role)).permissions
                if grant.allowed
            }
            if (
                context.actor_type != ActorType.PLATFORM_SUPPORT
                and not grants <= context.capabilities
            ):
                raise AuthorizationError()
            user.role_id = role_id
            await session.flush()
            await assert_custodian(session, context.tenant_id)
            audit(session, context, "member_role_assigned", "user", user_id)

    async def delete(self, context: RequestContext, identity: UUID, revision: int) -> None:
        async with self.sessions.begin() as session:
            await self._enter(session, context, Permission.ROLE_DELETE, write=True)
            role = await session.scalar(
                select(TenantRole)
                .where(TenantRole.tenant_id == context.tenant_id, TenantRole.id == identity)
                .with_for_update()
            )
            if role is None:
                raise NotFoundError()
            if role.revision != revision:
                raise RevisionConflictError()
            grants = {
                grant.key
                for grant in (await self._view(session, role)).permissions
                if grant.allowed
            }
            if (
                context.actor_type != ActorType.PLATFORM_SUPPORT
                and not grants <= context.capabilities
            ):
                raise AuthorizationError()
            if await session.scalar(
                select(User.id)
                .where(User.tenant_id == context.tenant_id, User.role_id == identity)
                .limit(1)
            ):
                raise ConflictError()
            await session.execute(
                delete(RolePermission).where(
                    RolePermission.tenant_id == context.tenant_id,
                    RolePermission.role_id == identity,
                )
            )
            await session.delete(role)
            audit(session, context, "role_deleted", "role", identity)
