"""Test data: a workspace with an owner who is already signed in, and platform admins.

Written with the schema owner's connection, the way the setup job writes the first
administrator. Nothing here is reachable from the HTTP application.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from uuid import UUID, uuid4, uuid5

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditAction
from app.audit.repository import AuditRepository
from app.audit.service import AuditService
from app.auth.context import ActorType, RequestContext
from app.auth.policies import Permission
from app.core.database.rls import set_tenant_scope
from app.identity.models import Account
from app.identity.passwords import hash_password
from app.identity.sessions import issue_session
from app.rbac.models import RolePermission, TenantRole
from app.rbac.service import seed_roles
from app.settings.store import seed_settings
from app.tenants.models import Tenant
from app.users.models import User

PASSWORD = "correct horse battery staple"
# A narrow role for tests that need someone who may look but not touch.
MEMBER_PERMISSIONS = (Permission.TENANT_READ, Permission.DIRECTORY_READ, Permission.KNOWLEDGE_READ)


def member_role(tenant_id: UUID) -> UUID:
    """The id of the test-only "Member" role in a workspace made by create_workspace."""
    return uuid5(tenant_id, "role:member")


@cache
def password_hash() -> str:
    # One slow hash for the whole test run.
    return hash_password(PASSWORD)


@dataclass(frozen=True)
class Workspace:
    tenant_id: UUID
    user_id: UUID
    account_id: UUID
    session_id: UUID
    token: SecretStr
    email: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token.get_secret_value()}"}


async def account_for(session: AsyncSession, email: str, display_name: str) -> Account:
    """The account for an address, created with the shared test password if missing."""
    normalized = email.strip().lower()
    account = await session.scalar(select(Account).where(Account.email == normalized))
    if account is None:
        account = Account(
            id=uuid4(),
            email=normalized,
            display_name=display_name,
            password_hash=password_hash(),
            is_platform_admin=False,
        )
        session.add(account)
        await session.flush()
    return account


async def create_workspace(
    admin: async_sessionmaker[AsyncSession],
    *,
    name: str,
    email: str,
    display_name: str = "Owner",
) -> Workspace:
    """A live workspace whose owner has accepted and holds a session working in it."""
    async with admin.begin() as session:
        tenant_id = uuid4()
        context = RequestContext(uuid4(), tenant_id, None, ActorType.SYSTEM)
        await set_tenant_scope(session, tenant_id)
        tenant = Tenant(id=tenant_id, name=name, service_state="live")
        session.add(tenant)
        await session.flush()
        roles = await seed_roles(session, tenant.id)
        session.add(TenantRole(id=member_role(tenant.id), tenant_id=tenant.id, name="Member"))
        await session.flush()
        session.add_all(
            [
                RolePermission(
                    tenant_id=tenant.id,
                    role_id=member_role(tenant.id),
                    key=item.value,
                    allowed=item in MEMBER_PERMISSIONS,
                )
                for item in Permission
            ]
        )
        await seed_settings(session, tenant)
        account = await account_for(session, email, display_name)
        user = User(
            id=uuid4(),
            tenant_id=tenant.id,
            account_id=account.id,
            email=account.email,
            display_name=display_name,
            role_id=roles["owner"],
            accepted_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()
        row, token = issue_session(session, account, timedelta(days=1))
        row.tenant_id = tenant.id
        audit = AuditService(AuditRepository(session, context))
        audit.record(context, AuditAction.TENANT_CREATED, "tenant", tenant.id)
        audit.record(context, AuditAction.USER_CREATED, "user", user.id)
        return Workspace(tenant.id, user.id, account.id, row.id, SecretStr(token), account.email)


async def add_member(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    email: str,
    display_name: str,
    role_id: UUID | None = None,
    user_id: UUID | None = None,
) -> User:
    """An accepted member of a workspace, with the account behind them."""
    account = await account_for(session, email, display_name)
    user = User(
        id=user_id or uuid4(),
        tenant_id=tenant_id,
        account_id=account.id,
        email=account.email,
        display_name=display_name,
        role_id=role_id or member_role(tenant_id),
        accepted_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()
    return user


@dataclass(frozen=True)
class PlatformAdmin:
    account_id: UUID
    session_id: UUID
    token: SecretStr
    email: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token.get_secret_value()}"}


async def create_platform_admin(
    admin: async_sessionmaker[AsyncSession], email: str | None = None
) -> PlatformAdmin:
    """A platform administrator who is already signed in."""
    async with admin.begin() as session:
        account = Account(
            id=uuid4(),
            email=(email or f"admin-{uuid4().hex}@platform.test").lower(),
            display_name="Platform Admin",
            password_hash=password_hash(),
            is_platform_admin=True,
        )
        session.add(account)
        await session.flush()
        row, token = issue_session(session, account, timedelta(hours=1))
        return PlatformAdmin(account.id, row.id, SecretStr(token), account.email)
